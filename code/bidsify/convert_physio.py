import os
import gzip
import json
import os.path as op
import numpy as np
import pandas as pd
import matplotlib.pyplot as plt
import nibabel as nib
from glob import glob
from copy import copy


class CouldNotFindThresholdError(Exception):
    """ Raised when a threshold for the gradient could not be found. 
    When processing many files, you can use this in a Try/Except clause. """
    pass


class PhilipsPhysioLog:
    """ Reads, converts, and aligns Philips physiology files (SCANPHYSLOG).
    Work in progress!
    """
    def __init__(self, f, sf=496, fmri_file=None, n_dyns=None, tr=None, manually_stopped=False):
        """ Initializes PhilipsPhysioLog object. 
        
        Parameters
        ----------
        f : str
            Path to SCANPHYSLOG file
        fmri_file : str
            Path to associated fMRI file. If None, n_dyns and tr should
            be set
        n_dyns : int
            Number of dynamics (volumes) of the associated fMRI scan
        sf : int
            Sampling frequency (samples per second)
        tr : int/float
            Time to repetition of fMRI scan (in seconds)
        manually_stopped : bool
            Whether the scan was manually stopped or not
        """
        self.f = f
        self.fmri_file = fmri_file
        self.sf = sf  # sampling freq
        self.n_dyns = n_dyns
        self.tr = tr  # TR in secs
        self.manually_stopped = manually_stopped
        
        if fmri_file is not None:
            img = nib.load(fmri_file)
            self.tr = img.header['pixdim'][4]
            self.n_dyns = img.header['dim'][4]

        if self.tr is None:
            raise ValueError("Please provide a TR or set fmri_file!")

        if self.n_dyns is None:
            raise ValueError("Please provide n_dyns or set fmri_file!")

        self.n_trig = n_dyns + 1 if manually_stopped else n_dyns
        self.trs = self.tr * self.sf
        self.time = None  # to be filled in later
        self.scan_time = None  # to be filled in later

    def load(self, marker_col=9, resp_cardiac_cols=(4, 5), grad_cols=(6, 7, 8)):
        """ Loads the SCANPHYSLOG and does some preprocessing.
        
        Parameters
        ----------
        marker_col : int
            Number of column with markers (0020, etc) in it
        """
        with open(self.f, 'r') as f_in:
            for line in f_in:
                if line[:2] != '##':
                    break  # stop here

            txt = f_in.readlines()
            # Remove weird pound signs, double spaces, and newline characters
            txt = [line.replace('  ', ' ').replace('\n', '') for line in txt if line != '#\n']
            self.markers = np.array([s.split(' ')[marker_col] for s in txt])

        m_start_idx = np.where(self.markers == '0100')[0]
        if len(m_start_idx) == 0:
            print("WARNING: didn't find a start marker ('0100') so setting it to 0.")
            m_start_idx = 0
        else:
            # Start one before the marker
            m_start_idx = m_start_idx[-1]

        m_end_idx = np.where(self.markers == '0020')[0]
        if len(m_end_idx) == 0:
            print("WARNING: didn't find an end marker ('0200')! "
                  "Setting it to the length of the file.")
            m_end_idx = len(txt) - 1
        else:
            m_end_idx = m_end_idx[-1]

        self.m_start_idx = m_start_idx  # marker start index
        self.m_end_idx = m_end_idx  # marker end index
        
        # Now, load in actual data, minus the markers (which messes things up)
        dat = np.loadtxt(self.f, dtype=int, usecols=np.arange(marker_col))
        self.grad = dat[:, grad_cols]  # set gradients
        self.respcard = dat[:, resp_cardiac_cols]  # resp/cardiac data
        self.n = self.grad.shape[0]  # number of timepoints
        self.time = np.arange(self.n) / self.sf  # in seconds
        
        return self  # for chaining

    def align(self, trigger_method='gradient_log', which_grad='y', trigger_diff_cutoff=5,
              offset_end_scan=166):
        """ Tries to find the onset of the volumes ('triggers') as to align
        the physio data with the fMRI volumes.
        Parameters
        ----------
        trigger_method : str
            Method to find triggers. Either 'gradient_log' (using the
            logged gradients), 'interpolate' (interpolate triggers from start to end),
            or 'vol_triggers' (using volume markers)
        which_grad : str
            Which gradient to use for finding triggers (only relevant for gradient_log)
        trigger_diff_cutoff : int
            Cutoff for detecting erroneous trigger diffs (time between triggers)
        offset_end_scan : int
            Assumed offset of the end of your scan and the actual end of your last volume.
            Unfortunately, this is not the same (thanks, Philips).
        """

        found_start_of_grad = False
        custom_end_idx = copy(self.m_end_idx)
        
        while not found_start_of_grad:
            # Go from end to start
            if self.grad[custom_end_idx, :].any():
                # If any of the gradients != 0, we found it
                found_start_of_grad = True
            else:  # reduce custom end index by 1
                custom_end_idx -= 1

            if custom_end_idx < 0:
                
                if trigger_method == 'gradient_log':
                    msg = ("ERROR: seems like gradients were not logged "
                           "but trigger_method='gradient_log'!")
                    raise ValueError(msg)

                # Just use end marker as "custom" end index
                custom_end_idx = self.m_end_idx
                break

        # c_end_idx = custom end index
        self.c_end_idx = custom_end_idx

        # Will be used in self._determine_triggers_by_*
        self.trigger_diff_cutoff = trigger_diff_cutoff

        # Do the actual hard work below (at least, for the gradient method)!
        if trigger_method == 'gradient_log':
            self._determine_triggers_by_gradient(which_grad)
        elif trigger_method == 'interpolate':
            self._determine_triggers_by_interpolation(offset_end_scan)
        elif trigger_method == 'vol_triggers':
            self._determine_triggers_by_volume_markers()
        else:
            raise ValueError("Please choose trigger_method from 'interpolate', 'vol_triggers', or 'gradient_log'")
        
        self.real_triggers = self.real_triggers.astype(int)

        # Check the time between triggers ("trigger_diffs")
        self.trigger_diffs = np.diff(np.r_[self.real_triggers, self.c_end_idx])
        
        # Check the diff of last vol. May not be completely accurate, but that's
        # fine usually
        diff_last_vol = (self.c_end_idx - self.real_triggers[-1])
        if np.abs(diff_last_vol - self.trs) > 10:
            # Notify user when the last trigger duration is off >10 samples
            diff_in_sec = diff_last_vol / self.sf
            print(f"WARNING: last trigger has a duration of {diff_in_sec:.2f} seconds!")
        
        # Weird triggers = those with a diff much larger/smaller than a TR
        # (except the last one)
        weird_triggers_idx = np.abs(self.trigger_diffs - self.trs) > trigger_diff_cutoff
        weird_triggers_idx[-1] = False  # always set last trigger to false        
        self.weird_triggers = self.real_triggers[weird_triggers_idx]

        n_weird = self.weird_triggers.size 
        if n_weird > 0:  # Notify user when there are weird triggers!
            weird_triggers_diff = self.trigger_diffs[weird_triggers_idx]        
            print(f"WARNING: found {n_weird} weird triggers with the following durations:")
            print(weird_triggers_diff)

        if self.manually_stopped:
            # If it was manually stopped, the last volume was not actually
            # recorded, so let's remove it
            self.real_triggers = self.real_triggers[:-1]
        
        n_real = self.real_triggers.size
        m_diff = self.trigger_diffs[:-1].mean() / self.sf 
        std_diff = self.trigger_diffs[:-1].std() / self.sf
        print(f"INFO: Found {n_real} triggers with a mean duration of "
              f"{m_diff:.5f} ({std_diff:.5f})!")

        # Define scan_time relative to first trigger (as BIDS needs)
        self.scan_time = self.time - self.time[self.real_triggers[0]]

        return self

    def _check_for_extra_triggers(self, real_triggers):
        """ Removes 'extra' (erroneous) triggers """
        trigger_diffs = np.r_[np.diff(real_triggers), self.trs]
        extra_idx = np.abs(self.trs - trigger_diffs) > self.trigger_diff_cutoff
        prob_extra = np.diff(np.r_[extra_idx.astype(int), 0]) == -1
        if prob_extra.sum() > 0:
            for idx in np.where(prob_extra)[0]:
                pre, post = trigger_diffs[idx-1], trigger_diffs[idx]
                if np.abs((pre + post) - (self.trs * 2)) < (self.trigger_diff_cutoff * 2):
                    print(f"WARNING: Interpolated extra trigger: {(pre / self.trs):.2f} (pre) "
                          f" and {(post / self.trs):.2f} (post)")
                    real_triggers = real_triggers[real_triggers != real_triggers[idx]]
                    real_triggers = np.r_[real_triggers, real_triggers[idx-1] + self.trs]

            real_triggers.sort()

        return real_triggers

    def _check_for_missed_triggers(self, real_triggers):
        """ Interpolates extra triggers when apparently missed. """
        trigger_diffs = np.diff(np.r_[real_triggers, self.c_end_idx])

        # When a trigger diff is close to 2 * TR * sf, it probably missed exactly one,
        # which is in the middle of the preceding and the next trigger
        diff_two_trs = np.abs(trigger_diffs - (self.trs * 2)) < (self.trigger_diff_cutoff * 2)
        prob_missed = real_triggers[diff_two_trs]
        for trig in prob_missed:
            interpolated_trigger = trig + self.trs
            real_triggers = np.r_[real_triggers, interpolated_trigger]

        real_triggers.sort()
        return real_triggers

    def _determine_triggers_by_volume_markers(self):
        """ Determines triggers by volume markers. """

        # Find the indices of volume markers
        idx_init_triggers = np.logical_or(self.markers == '0200', self.markers == '0202')
        init_triggers = np.where(idx_init_triggers)[0]

        if len(init_triggers) == 0:
            raise ValueError("ERROR: did not find any volume markers ('0200' or '0202')!")

        # Just to make sure, check the diffs
        init_triggers = self._check_for_missed_triggers(init_triggers)
        init_triggers = self._check_for_extra_triggers(init_triggers)

        # There may be more triggers (e.g., dummies) than dynamics,
        # so remove all 'excess' triggers
        real_triggers = init_triggers[-self.n_trig:]
        n_real = len(real_triggers)
        n_init = len(init_triggers)

        # Check to be sure
        if n_real != self.n_trig:
            raise ValueError(
                f"ERROR: expected to find {self.n_dyns} triggers, but found {n_real} "
                f"(and {n_init} init triggers)"
            )

        self.real_triggers = real_triggers

    def _determine_triggers_by_interpolation(self, offset_end_scan):
        """ Determine triggers by interpolation from (assumed)
        end of scan to begin of scan. Only use when you're really desperate. """

        if self.manually_stopped:
            print("WARNING: using the interpolation method with manually stopped scans "
                  "is a very bad idea!")

        # We assume the last volume ended by the end of the gradient minus
        # some offset
        assumed_end_last_vol = self.c_end_idx - offset_end_scan 
        
        # Backtrack to start
        assumed_start = int(assumed_end_last_vol - (self.trs * self.n_trig))

        # Okay, this is stupid, but necessary. Sometimes, people (like me) have
        # weird TRs, like 1.317. Now, we need to equally space these TRs as well
        # as possible, i.e., each TR should have approximately an equal amount of
        # samples. This is not exactly possible when the samples per TR is not an
        # integer. Let's calculate the modulo(self.trs, 1)
        leftover = int(10 * np.round(self.trs % 1, 1))
        
        # This is probably too complicated, but it works.
        diffs = np.diff(np.round(np.arange(self.n_trig + 1) * self.trs)) 
        diffs[0] += assumed_start  # add assumed start
        self.real_triggers = np.cumsum(diffs)  # magic
    
    def _determine_triggers_by_gradient(self, which_grad):
        """ Determine triggers by thresholding the gradient. 
        Very often works, but fails when the gradients are funky,
        e.g., when your FOV is extremely tilted or so. """

        # align_grad = data from gradient that is actually used
        self.align_grad = self.grad[:, {'x': 0, 'y': 1, 'z': 2}[which_grad]]
        # set prescan stuff to zero
        self.approx_start = self.c_end_idx - (self.trs * self.n_trig) - self.trs * 0.05
        grad = self.align_grad.copy()  # we want to plot everything, incl. prescan, later
        grad[np.arange(self.n) < self.approx_start] = 0
        
        thr = self.align_grad.min()
        while True:
            # Find potential triggers
            real_triggers = np.where(grad < thr)[0].astype(int)
            trigger_diffs = np.diff(np.r_[real_triggers, self.c_end_idx])
            real_triggers = real_triggers[trigger_diffs > 2]  # remove doubles
            
            # Check for missed triggers, but only when we're close to 
            # the expected number of dyns
            if (self.n_dyns - real_triggers.size) < 10:
                real_triggers = self._check_for_missed_triggers(real_triggers)
                real_triggers = self._check_for_extra_triggers(real_triggers)

            if real_triggers.size == self.n_trig:
                break # Found it!

            thr += 1
            if thr > 0:  # shit, didn't find it
                raise CouldNotFindThresholdError("Could not find threshold!")

        self.real_triggers = real_triggers

    def _generate_array(self):
        """ Generates an Nx3 array with
        (cardiac, resp, trigger) data. """
        vol_triggers = np.zeros(self.n)
        vol_triggers[self.real_triggers] = 1
        data = np.c_[self.respcard, vol_triggers]
        return data

    def to_bids(self, out_dir=None):
        """ Saves the data in BIDS format to disk. """

        if out_dir is None:
            # Save into same dir as SCANPHYSLOG
            out_dir = op.dirname(self.f)
        
        if not op.isdir(out_dir):
            os.makedirs(out_dir, exist_ok=True)

        base_name, _ = op.splitext(op.basename(self.f))
        base_name = op.join(out_dir, base_name)

        info = {
           "SamplingFrequency": self.sf,
           "StartTime": self.scan_time[0],
           "Columns": ["cardiac", "respiratory", "trigger"]
        }

        # Save json sidecar metadata
        with open(f'{base_name}.json', "w") as write_file:
            json.dump(info, write_file, indent=4)

        # Get actual data (cardiac, resp, triggers) and save
        data = self._generate_array()

        for i, name in enumerate(['cardiac', 'respiratory']):
            trace = data[:, i]
            if np.sum(trace) == 0:
                print(f"WARNING: the {name} trace is empty!")

        tsv_out = f'{base_name}.tsv'
        np.savetxt(tsv_out, data, delimiter='\t')

        # Zip the data (BIDS needs .tsv.gz files)        
        with open(tsv_out, 'rb') as f_in, gzip.open(tsv_out + '.gz', 'wb') as f_out:
            print(f"INFO: Saving BIDS data to {tsv_out} ...")
            f_out.writelines(f_in)
        
        # Remove old (unzipped) tsv file
        os.remove(tsv_out)

    def plot_traces(self, standardize=True, out_dir=None):
        """ Plots the cardiac/resp/trigger data for QC. 
        Parameters
        ----------
        standardize : bool
            Whether to standardize ((x - xmean) / xstd) the data before
            plotting
        out_dir : str
            Output directory (if None, same as SCANPHYSLOG)
        """

        if out_dir is None:
            out_dir = op.dirname(self.f)

        data = self._generate_array()
        columns = ['Cardiac trace', 'Respiratory trace', 'Volume triggers']
        df = pd.DataFrame(data, columns=columns, index=self.time)
        
        if standardize:
            for col in ['Cardiac trace', 'Respiratory trace']:
                df[col] = (df[col] - df[col].mean()) / df[col].std()

        fig, axes = plt.subplots(figsize=(20, 10), nrows=3, sharex=True, sharey=False)
        for i, ax in enumerate(axes):
            ax.plot(df.iloc[:, i], lw=1)
            ax.set_xlim(0, df.index[-1])
            ax.set_title(df.columns[i], fontsize=20)

            if i == 2:
                ax.set_xlabel('Time (in samples)', fontsize=15)

            if i != 2:  # Grid makes the triggers hard to read
                ax.grid()

        fig.tight_layout()

        if not op.isdir(out_dir):
            os.makedirs(out_dir, exist_ok=True)

        base_name, _ = op.splitext(op.basename(self.f))
        base_name = op.join(out_dir, base_name)
        fig.savefig(f'{base_name}_traces.png', dpi=100)
        plt.close()

    def plot_alignment(self, win=8, before_start=5, out_dir=None):
        """ Plots the gradient/alignment/weird triggers.
        Parameters
        ----------
        win : int
            Window to plot (in seconds)
        before_start : int
            How many seconds before the start to include in plot (in seconds)
        out_dir : str
            Output directory
        """

        if out_dir is None:
            out_dir = op.dirname(self.f)

        win = int(win * self.sf)
        before_start = int(before_start * self.sf)

        n_weird = len(self.weird_triggers)
        if n_weird > 5:
            print(f"WARNING: found {n_weird} weird triggers! Only going to plot the first 5")
            n_weird = 5

        fig, ax = plt.subplots(nrows=(4 + n_weird), figsize=(30, 9 + (3 * n_weird)))

        trig_amp = 1
        if hasattr(self, 'align_grad'):
            # Make sure triggers are visible
            trig_amp = self.align_grad.min() * .25

        # Define trace of triggers (0, 0, 0, amp, 0, 0, 0, etc)
        trigger_trace = np.zeros(self.n)
        trigger_trace[self.real_triggers] = trig_amp

        # Also define a "trace" with start and end marker (and custom)
        start_end = np.zeros(self.n)
        start_end[self.m_start_idx] = trig_amp
        start_end[self.m_end_idx] = trig_amp
        start_end[self.c_end_idx] = trig_amp * 2

        windows = [  # windows to plot
            ('Full', np.arange(self.n)[(self.m_start_idx-before_start):]),
            ('Start', np.arange(self.m_start_idx, self.m_start_idx + win)),
            ('End', np.arange(self.m_end_idx - win, self.m_end_idx)),
        ]

        for weird_trig in self.weird_triggers[:n_weird]:
            trig_nr = np.where(weird_trig == self.real_triggers)[0][0]
            trig_sec = weird_trig / self.sf
            trig_window = np.arange(
                weird_trig - win,
                np.min([weird_trig + win, self.c_end_idx])
            ).astype(int)
            
            windows.append(
                (f'Weird trigger (#{trig_nr+1}) at {weird_trig} ({trig_sec} sec.)',
                trig_window     
            ))

        for i, (title, period) in enumerate(windows):

            ext_space = 300 if i == 0 else 50
            lw_trigs = 1 if i == 0 else 2  # linewidth
            legend = []
            if hasattr(self, 'align_grad'):
                ax[i].plot(period, self.align_grad[period], lw=0.5)
                legend.append('grad')

            ax[i].plot(period, start_end[period], lw=2)
            legend.append('start/end')
            
            ax[i].plot(period, trigger_trace[period], lw=lw_trigs)
            ax[i].set_title(title, fontsize=15)
            ax[i].set_xlim(period[0] - ext_space, period[-1] + ext_space)
            ax[i].legend(legend + ['triggers'])
            if i > 2:
                ax[i].axvline(self.weird_triggers[i-3], ls='--', c='k', lw=0.8)
                
            if hasattr(self, 'approx_start') and i == 0:
                ax[0].axvline(self.approx_start, ls='--', c='k', lw=0.5)

        ax[-1].plot(self.trigger_diffs[:-1])
        ax[-1].set_title('Number of samples between triggers', fontsize=15)
        m_diff = np.mean(self.trigger_diffs[:-1])
        std_diff = np.std(self.trigger_diffs[:-1])
        txt = f'm = {m_diff:.0f} ({(m_diff / self.sf):.3f}), std = {std_diff:.0f} ({(std_diff / self.sf):.3f})'
        ax[-1].text(0, self.trigger_diffs[:-1].max(), txt, fontsize=20)
        fig.tight_layout()

        if not op.isdir(out_dir):
            os.makedirs(out_dir, exist_ok=True)

        base_name, _ = op.splitext(op.basename(self.f))
        base_name = op.join(out_dir, base_name + '_alignment.png')

        if n_weird > 0:  # for debugging purposes
            base_name = base_name.replace('.png', '_CHECKOUT.png')
        
        fig.savefig(base_name, dpi=100)
        plt.close()


if __name__ == '__main__':
    import numpy as np
    import os.path as op
    import joblib as jl
    import nibabel as nib
    from glob import glob
    from joblib import Parallel, delayed

    def _run_parallel(log):
        sub_name, ses_name = op.basename(log).split("_")[:2]
        trigger_method = 'gradient_log'  # or: 'interpolation', 'vol_triggers'
        nii = log.replace('_recording-respcardiac_physio.log', '_bold.nii.gz')
        vols = nib.load(nii).shape[-1]
        tr = np.round(nib.load(nii).header['pixdim'][4], 3)
        print(f'\nProcessing {log}: dyns={vols}, TR={tr:.3f}, method={trigger_method}')
    
        try:
            phlog = PhilipsPhysioLog(f=log, tr=tr, n_dyns=vols, sf=496, manually_stopped=False)  # init
            phlog.load()
            phlog.align(trigger_method=trigger_method)  # load and find vol triggers
            out_dir = op.join(f'../derivatives/physiology/{sub_name}/{ses_name}/figures')
            phlog.plot_alignment(out_dir=out_dir)  # plots alignment with gradient
            phlog.to_bids()  # writes out .tsv.gz and .json files
            phlog.plot_traces(out_dir=out_dir)
            to_return = None
        except CouldNotFindThresholdError:  # something went wrong with gradient-based alignment
            print(f"Could not find threshold for {log}")
            to_return = log

        return to_return


    logs = sorted(glob('../sub-*/ses-*/func/*physio.log'))
    Parallel(n_jobs=10)(delayed(_run_parallel)(log) for log in logs)
