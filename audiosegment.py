"""
This module simply exposes a wrapper of a pydub.AudioSegment object.
"""
from __future__ import division
from __future__ import print_function

import collections
import itertools
import math
import numpy as np
import pydub
import os
import random
import subprocess
import sys
import tempfile
import webrtcvad

MS_PER_S = 1000
S_PER_MIN = 60
MS_PER_MIN = MS_PER_S * S_PER_MIN

class AudioSegment:
    """
    This class is a wrapper for a pydub.AudioSegment that provides additional methods.
    """

    def __init__(self, pydubseg, name):
        self.seg = pydubseg
        self.name = name

    def __getattr__(self, attr):
        orig_attr = self.seg.__getattribute__(attr)
        if callable(orig_attr):
            def hooked(*args, **kwargs):
                result = orig_attr(*args, **kwargs)
                if result == self.seg:
                    return self
                elif type(result) == pydub.AudioSegment:
                    return AudioSegment(result, self.name)
                else:
                    return  result
            return hooked
        else:
            return orig_attr

    def __len__(self):
        return len(self.seg)

    def __eq__(self, other):
        return self.seg == other

    def __ne__(self, other):
        return self.seg != other

    def __iter__(self, other):
        return (x for x in self.seg)

    def __getitem__(self, millisecond):
        return AudioSegment(self.seg[millisecond], self.name)

    def __add__(self, arg):
        if type(arg) == AudioSegment:
            self.seg._data = self.seg._data + arg.seg._data
        else:
            self.seg = self.seg + arg
        return self

    def __radd__(self, rarg):
        return self.seg.__radd__(rarg)

    def __repr__(self):
        return str(self)

    def __str__(self):
        return str(self.get_array_of_samples())

    def __sub__(self, arg):
        if type(arg) == AudioSegment:
            self.seg = self.seg - arg.seg
        else:
            self.seg = self.seg - arg
        return self

    def __mul__(self, arg):
        if type(arg) == AudioSegment:
            self.seg = self.seg * arg.seg
        else:
            self.seg = self.seg * arg
        return self

    def detect_voice(self, prob_detect_voice=0.5):
        """
        Returns self as a list of tuples:
        [('v', voiced segment), ('u', unvoiced segment), (etc.)]

        The overall order of the AudioSegment is preserved.

        :param prob_detect_voice: The raw probability that any random 20ms window of the audio file
                                  contains voice.
        :returns: The described list.
        """
        assert self.frame_rate in (48000, 32000, 16000, 8000), "Try resampling to one of the allowed frame rates."
        assert self.sample_width == 2, "Try resampling to 16 bit."
        assert self.channels == 1, "Try resampling to one channel."

        class model_class:
            def __init__(self, aggressiveness):
                self.v = webrtcvad.Vad(int(aggressiveness))

            def predict(self, vector):
                if self.v.is_speech(vector.raw_data, vector.frame_rate):
                    return 1
                else:
                    return 0

        model = model_class(aggressiveness=2)
        pyesno = 0.3  # Probability of the next 20 ms being unvoiced given that this 20 ms was voiced
        pnoyes = 0.2  # Probability of the next 20 ms being voiced given that this 20 ms was unvoiced
        p_realyes_outputyes = 0.4  # WebRTCVAD has a very high FP rate - just because it says yes, doesn't mean much
        p_realyes_outputno  = 0.05  # If it says no, we can be very certain that it really is a no
        p_yes_raw = prob_detect_voice
        filtered = self.detect_event(model=model,
                                     ms_per_input=20,
                                     transition_matrix=(pyesno, pnoyes),
                                     model_stats=(p_realyes_outputyes, p_realyes_outputno),
                                     event_length_s=0.25,
                                     prob_raw_yes=p_yes_raw)
        ret = []
        for tup in filtered:
            t = ('v', tup[1]) if tup[0] == 'y' else ('u', tup[1])
            ret.append(t)
        return ret

    def dice(self, seconds, zero_pad=False):
        """
        Cuts the AudioSegment into `seconds` segments (at most). So for example, if seconds=10,
        this will return a list of AudioSegments, in order, where each one is at most 10 seconds
        long. If `zero_pad` is True, the last item AudioSegment object will be zero padded to result
        in `seconds` seconds.

        :param seconds: The length of each segment in seconds. Can be either a float/int, in which case
                        `self.duration_seconds` / `seconds` are made, each of `seconds` length, or a
                        list-like can be given, in which case the given list must sum to
                        `self.duration_seconds` and each segment is specified by the list - e.g.
                        the 9th AudioSegment in the returned list will be `seconds[8]` seconds long.
        :param zero_pad: Whether to zero_pad the final segment if necessary. Ignored if `seconds` is
                         a list-like.
        :returns: A list of AudioSegments, each of which is the appropriate number of seconds long.
        :raises: ValueError if a list-like is given for `seconds` and the list's durations do not sum
                 to `self.duration_seconds`.
        """
        try:
            total_s = sum(seconds)
            if not (self.duration_seconds <= total_s + 1 and self.duration_seconds >= total_s - 1):
                raise ValueError("`seconds` does not sum to within one second of the duration of this AudioSegment.\
                                 given total seconds: %s and self.duration_seconds: %s" % (total_s, self.duration_seconds))
            starts = []
            stops = []
            time_ms = 0
            for dur in seconds:
                starts.append(time_ms)
                time_ms += dur * MS_PER_S
                stops.append(time_ms)
            zero_pad = False
        except TypeError:
            # `seconds` is not a list
            starts = range(0, int(round(self.duration_seconds * MS_PER_S)), int(round(seconds * MS_PER_S)))
            stops = (min(self.duration_seconds * MS_PER_S, start + seconds * MS_PER_S) for start in starts)
        outs = [self[start:stop] for start, stop in zip(starts, stops)]
        out_lens = [out.duration_seconds for out in outs]
        # Check if our last slice is within one ms of expected - if so, we don't need to zero pad
        if zero_pad and not (out_lens[-1] <= seconds * MS_PER_S + 1 and out_lens[-1] >= seconds * MS_PER_S - 1):
            num_zeros = self.frame_rate * (seconds * MS_PER_S - out_lens[-1])
            outs[-1] = outs[-1].zero_extend(num_samples=num_zeros)
        return outs

    def detect_event(self, model, ms_per_input, transition_matrix, model_stats, event_length_s,
                     start_as_yes=False, prob_raw_yes=0.5):
        """
        A list of tuples of the form [('n', AudioSegment), ('y', AudioSegment), etc.] is returned, where tuples
        of the form ('n', AudioSegment) are the segments of sound where the event was not detected,
        while ('y', AudioSegment) tuples were the segments of sound where the event was detected.

        :param model:               The model. The model must have a predict() function which takes an AudioSegment
                                    of `ms_per_input` number of ms and which outputs 1 if the audio event is detected
                                    in that input, and 0 if not. Make sure to resample the AudioSegment to the right
                                    values before calling this function on it.

        :param ms_per_input:        The number of ms of AudioSegment to be fed into the model at a time. If this does not
                                    come out even, the last AudioSegment will be zero-padded.

        :param transition_matrix:   An iterable of the form: [p(yes->no), p(no->yes)]. That is, the probability of moving
                                    from a 'yes' state to a 'no' state and the probability of vice versa.

        :param model_stats:         An iterable of the form: [p(reality=1|output=1), p(reality=1|output=0)]. That is,
                                    the probability of the ground truth really being a 1, given that the model output a 1,
                                    and the probability of the ground truth being a 1, given that the model output a 0.

        :param event_length_s:      The typical duration of the event you are looking for in seconds (can be a float).

        :param start_as_yes:        If True, the first `ms_per_input` will be in the 'y' category. Otherwise it will be
                                    in the 'n' category.

        :param prob_raw_yes:        The raw probability of finding the event in any given `ms_per_input` vector.

        :returns:                   A list of tuples of the form [('n', AudioSegment), ('y', AudioSegment), etc.],
                                    where over the course of the list, the AudioSegment in tuple 3 picks up
                                    where the one in tuple 2 left off.

        :raises:                    ValueError if `ms_per_input` is negative or larger than the number of ms in this
                                    AudioSegment; if `transition_matrix` or `model_stats` do not have a __len__ attribute
                                    or are not length 2; if the values in `transition_matrix` or `model_stats` are not
                                    in the closed interval [0.0, 1.0].
        """
        if ms_per_input < 0 or ms_per_input / MS_PER_S > self.duration_seconds:
            raise ValueError("ms_per_input cannot be negative and cannot be longer than the duration of the AudioSegment."\
                             " The given value was " + str(ms_per_input))
        elif not hasattr(transition_matrix, "__len__") or len(transition_matrix) != 2:
            raise ValueError("transition_matrix must be an iterable of length 2.")
        elif not hasattr(model_stats, "__len__") or len(model_stats) != 2:
            raise ValueError("model_stats must be an iterable of length 2.")
        elif any([True for prob in transition_matrix if prob > 1.0 or prob < 0.0]):
            raise ValueError("Values in transition_matrix are probabilities, and so must be in the range [0.0, 1.0].")
        elif any([True for prob in model_stats if prob > 1.0 or prob < 0.0]):
            raise ValueError("Values in model_stats are probabilities, and so must be in the range [0.0, 1.0].")
        elif prob_raw_yes > 1.0 or prob_raw_yes < 0.0:
            raise ValueError("`prob_raw_yes` is a probability, and so must be in the range [0.0, 1.0]")

        # Get the yeses or nos for when the filter is triggered (when the event is on/off)
        filter_indices = [yes_or_no for yes_or_no in self._get_filter_indices(start_as_yes,
                                                                              prob_raw_yes,
                                                                              ms_per_input,
                                                                              model,
                                                                              transition_matrix,
                                                                              model_stats)]
        # Run a homogeneity filter over the values to make local regions more self-similar (reduce noise)
        ret = self._homogeneity_filter(filter_indices, window_size=int(round(0.25 * MS_PER_S / ms_per_input)))
        # Group the consecutive ones together
        ret = self._group_filter_values(ret, ms_per_input)
        # Take the groups and turn them into AudioSegment objects
        real_ret = self._reduce_filtered_segments(ret)

        return real_ret

    def _get_filter_indices(self, start_as_yes, prob_raw_yes, ms_per_input, model, transition_matrix, model_stats):
        """
        This has been broken out of the `filter` function to reduce cognitive load.
        """
        filter_triggered = 1 if start_as_yes else 0
        prob_raw_no = 1.0 - prob_raw_yes
        for segment, _timestamp in self.generate_frames_as_segments(ms_per_input):
            yield filter_triggered
            observation = int(round(model.predict(segment)))
            assert observation == 1 or observation == 0, "The given model did not output a 1 or a 0, output: "\
                   + str(observation)
            prob_hyp_yes_given_last_hyp = 1.0 - transition_matrix[0] if filter_triggered else transition_matrix[1]
            prob_hyp_no_given_last_hyp  = transition_matrix[0] if filter_triggered else 1.0 - transition_matrix[1]
            prob_hyp_yes_given_data = model_stats[0] if observation == 1 else model_stats[1]
            prob_hyp_no_given_data = 1.0 - model_stats[0] if observation == 1 else 1.0 - model_stats[1]
            hypothesis_yes = prob_raw_yes * prob_hyp_yes_given_last_hyp * prob_hyp_yes_given_data
            hypothesis_no  = prob_raw_no * prob_hyp_no_given_last_hyp  * prob_hyp_no_given_data
            # make a list of ints - each is 0 or 1. The number of 1s is hypotheis_yes * 100
            # the number of 0s is hypothesis_no * 100
            distribution = [1 for i in range(int(round(hypothesis_yes * 100)))]
            distribution.extend([0 for i in range(int(round(hypothesis_no * 100)))])
            # shuffle
            random.shuffle(distribution)
            filter_triggered = random.choice(distribution)

    def _group_filter_values(self, filter_indices, ms_per_input):
        """
        This has been broken out of the `filter` function to reduce cognitive load.
        """
        ret = []
        for filter_value, (_segment, timestamp) in zip(filter_indices, self.generate_frames_as_segments(ms_per_input)):
            if filter_value == 1:
                if len(ret) > 0 and ret[-1][0] == 'n':
                    ret.append(['y', timestamp])  # The last one was different, so we create a new one
                elif len(ret) > 0 and ret[-1][0] == 'y':
                    ret[-1][1] = timestamp  # The last one was the same as this one, so just update the timestamp
                else:
                    ret.append(['y', timestamp])  # This is the first one
            else:
                if len(ret) > 0 and ret[-1][0] == 'n':
                    ret[-1][1] = timestamp
                elif len(ret) > 0 and ret[-1][0] == 'y':
                    ret.append(['n', timestamp])
                else:
                    ret.append(['n', timestamp])
        return ret

    def _homogeneity_filter(self, ls, window_size):
        """
        This has been broken out of the `filter` function to reduce cognitive load.

        ls is a list of 1s or 0s for when the filter is on or off
        """
        k = window_size
        i = k
        while i <= len(ls) - k:
            # Get a window of k items
            window = [ls[i + j] for j in range(k)]
            # Change the items in the window to be more like the mode of that window
            mode = 1 if sum(window) >= k / 2 else 0
            for j in range(k):
                ls[i+j] = mode
            i += k
        return ls

    def _reduce_filtered_segments(self, ret):
        """
        This has been broken out of the `filter` function to reduce cognitive load.
        """
        real_ret = []
        for i, (this_yesno, next_timestamp) in enumerate(ret):
            if i > 0:
                _next_yesno, timestamp = ret[i - 1]
            else:
                timestamp = 0

            data = self[timestamp * MS_PER_S:next_timestamp * MS_PER_S].raw_data
            seg = AudioSegment(pydub.AudioSegment(data=data, sample_width=self.sample_width,
                                                  frame_rate=self.frame_rate, channels=self.channels), self.name)
            real_ret.append((this_yesno, seg))
        return real_ret

    def filter_silence(self, duration_s=1, threshold_percentage=1, console_output=False):
        """
        Returns a copy of this AudioSegment, but whose silence has been removed.

        .. note:: This method requires that you have the program 'sox' installed.

        .. warning:: This method uses the program 'sox' to perform the task. While this is very fast for a single
                     function call, the IO may add up for a large numbers of AudioSegment objects.

        :param duration_s: The number of seconds of "silence" that must be present in a row to
                           be stripped.
        :param threshold_percentage: Silence is defined as any samples whose absolute value is below
                                     `threshold_percentage * max(abs(samples in this segment))`.
        :param console_output: If True, will pipe all sox output to the console.
        :returns: A copy of this AudioSegment, but whose silence has been removed.
        """
        tmp = tempfile.NamedTemporaryFile()
        othertmp = tempfile.NamedTemporaryFile()
        self.export(tmp.name, format="WAV")
        command = "sox " + tmp.name + " -t wav " + othertmp.name + " silence -l 1 0.1 "\
                   + str(threshold_percentage) + "% -1 " + str(float(duration_s)) + " " + str(threshold_percentage) + "%"
        stdout = stderr = subprocess.PIPE if console_output else subprocess.DEVNULL
        res = subprocess.run(command.split(' '), stdout=stdout, stderr=stderr)
        assert res.returncode == 0, "Sox did not work as intended, or perhaps you don't have Sox installed?"
        other = AudioSegment(pydub.AudioSegment.from_wav(othertmp.name), self.name)
        tmp.close()
        othertmp.close()
        return other

    def fft(self, start_s=None, duration_s=None, start_sample=None, num_samples=None, zero_pad=False):
        """
        Transforms the indicated slice of the AudioSegment into the frequency domain and returns the bins
        and the values.

        If neither `start_s` or `start_sample` is specified, the first sample of the slice will be the first sample
        of the AudioSegment.

        If neither `duration_s` or `num_samples` is specified, the slice will be from the specified start
        to the end of the segment.

        :param start_s: The start time in seconds. If this is specified, you cannot specify `start_sample`.
        :param duration_s: The duration of the slice in seconds. If this is specified, you cannot specify `num_samples`.
        :param start_sample: The zero-based index of the first sample to include in the slice.
                             If this is specified, you cannot specify `start_s`.
        :param num_samples: The number of samples to include in the slice. If this is specified, you cannot
                            specify `duration_s`.
        :param zero_pad: If True and the combination of start and duration result in running off the end of
                         the AudioSegment, the end is zero padded to prevent this.
        :returns: np.ndarray of frequencies, np.ndarray of amount of each frequency
        :raises: ValueError If `start_s` and `start_sample` are both specified and/or if both `duration_s` and
                            `num_samples` are specified.
        """
        if start_s is not None and start_sample is not None:
            raise ValueError("Only one of start_s and start_sample can be specified.")
        if duration_s is not None and num_samples is not None:
            raise ValueError("Only one of duration_s and num_samples can be specified.")
        if start_s is None and start_sample is None:
            start_sample = 0
        if duration_s is None and num_samples is None:
            num_samples = len(self.get_array_of_samples()) - int(start_sample)

        if duration_s is not None:
            num_samples = int(round(duration_s * self.frame_rate))
        if start_s is not None:
            start_sample = int(round(start_s * self.frame_rate))

        end_sample = start_sample + num_samples  # end_sample is excluded
        if end_sample > len(self.get_array_of_samples()) and not zero_pad:
            raise ValueError("The combination of start and duration will run off the end of the AudioSegment object.")
        elif end_sample > len(self.get_array_of_samples()) and zero_pad:
            arr = np.array(self.get_array_of_samples())
            zeros = np.zeros(end_sample - len(arr))
            arr = np.append(arr, zeros)
        else:
            arr = np.array(self.get_array_of_samples())

        audioslice = np.array(arr[start_sample:end_sample])
        fft_result = np.fft.fft(audioslice)[range(int(round(num_samples/2)) + 1)]
        bins = np.arange(0, int(round(num_samples/2)) + 1, 1.0) * (self.frame_rate / num_samples)
        return bins, fft_result

    def generate_frames(self, frame_duration_ms, zero_pad=True):
        """
        Yields self's data in chunks of frame_duration_ms.

        This function adapted from pywebrtc's example [https://github.com/wiseman/py-webrtcvad/blob/master/example.py].

        :param frame_duration_ms: The length of each frame in ms.
        :param zero_pad: Whether or not to zero pad the end of the AudioSegment object to get all
                         the audio data out as frames. If not, there may be a part at the end
                         of the Segment that is cut off (the part will be <= `frame_duration_ms` in length).
        :returns: A Frame object with properties 'bytes (the data)', 'timestamp (start time)', and 'duration'.
        """
        Frame = collections.namedtuple("Frame", "bytes timestamp duration")

        # (samples/sec) * (seconds in a frame) * (bytes/sample)
        bytes_per_frame = int(self.frame_rate * (frame_duration_ms / 1000) * self.sample_width)
        offset = 0  # where we are so far in self's data (in bytes)
        timestamp = 0.0  # where we are so far in self (in seconds)
        # (bytes/frame) * (sample/bytes) * (sec/samples)
        frame_duration_s = (bytes_per_frame / self.frame_rate) / self.sample_width
        while offset + bytes_per_frame < len(self.raw_data):
            yield Frame(self.raw_data[offset:offset + bytes_per_frame], timestamp, frame_duration_s)
            timestamp += frame_duration_s
            offset += bytes_per_frame

        if zero_pad:
            rest = self.raw_data[offset:]
            zeros = bytes(bytes_per_frame - len(rest))
            yield Frame(rest + zeros, timestamp, frame_duration_s)

    def generate_frames_as_segments(self, frame_duration_ms, zero_pad=True):
        """
        Does the same thing as `generate_frames`, but yields tuples of (AudioSegment, timestamp) instead of Frames.
        """
        for frame in self.generate_frames(frame_duration_ms, zero_pad=zero_pad):
            seg = AudioSegment(pydub.AudioSegment(data=frame.bytes, sample_width=self.sample_width,
                               frame_rate=self.frame_rate, channels=self.channels), self.name)
            yield seg, frame.timestamp

    def reduce(self, others):
        """
        Reduces others into this one by concatenating all the others onto this one and
        returning the result. Does not modify self, instead, makes a copy and returns that.

        :param others: The other AudioSegment objects to append to this one.
        :returns: The concatenated result.
        """
        ret = AudioSegment(self.seg, "")
        selfdata = [self.seg._data]
        otherdata = [o.seg._data for o in others]
        ret.seg._data = b''.join(selfdata + otherdata)

        return ret

    def resample(self, sample_rate_Hz=None, sample_width=None, channels=None, console_output=False):
        """
        Returns a new AudioSegment whose data is the same as this one, but which has been resampled to the
        specified characteristics. Any parameter left None will be unchanged.

        .. note:: This method requires that you have the program 'sox' installed.

        .. warning:: This method uses the program 'sox' to perform the task. While this is very fast for a single
                     function call, the IO may add up for a large numbers of AudioSegment objects.

        :param sample_rate_Hz: The new sample rate in Hz.
        :param sample_width: The new sample width in bytes, so sample_width=2 would correspond to 16 bit (2 byte) width.
        :param channels: The new number of channels.
        :param console_output: Will print the output of sox to the console if True.
        :returns: The newly sampled AudioSegment.
        """
        if sample_rate_Hz is None:
            sample_rate_Hz = self.frame_rate
        if sample_width is None:
            sample_width = self.sample_width
        if channels is None:
            channels = self.channels

        infile, outfile = tempfile.NamedTemporaryFile(), tempfile.NamedTemporaryFile()
        self.export(infile.name, format="wav")
        command = "sox " + infile.name + " -b" + str(sample_width * 8) + " -r " + str(sample_rate_Hz) + " -t wav " + outfile.name + " channels " + str(channels)
        stdout = stderr = subprocess.PIPE if console_output else subprocess.DEVNULL
        res = subprocess.run(command.split(' '), stdout=stdout, stderr=stderr)
        res.check_returncode()
        other = AudioSegment(pydub.AudioSegment.from_wav(outfile.name), self.name)
        infile.close()
        outfile.close()
        return other

    def spectrogram(self, start_s=None, duration_s=None, start_sample=None, num_samples=None,
                    window_length_s=None, window_length_samples=None, overlap=0.5):
        """
        Does a series of FFTs from `start_s` or `start_sample` for `duration_s` or `num_samples`.
        Effectively, transforms a slice of the AudioSegment into the frequency domain across different
        time bins.

        .. code-block:: python

            # Example for plotting a spectrogram using this function
            import audiosegment
            import matplotlib.pyplot as plt
            import numpy as np

            seg = audiosegment.from_file("somebodytalking.wav")
            hist_bins, times, amplitudes = seg.spectrogram(start_s=4.3, duration_s=1, window_length_s=0.03, overlap=0.5)
            hist_bins_khz = hist_bins / 1000
            amplitudes_real_normed = np.abs(amplitudes) / len(amplitudes)
            amplitudes_logged = 10 * np.log10(amplitudes_real_normed + 1e-9)  # for numerical stability
            x, y = np.mgrid[:len(times), :len(hist_bins_khz)]
            fig, ax = plt.subplots()
            ax.pcolormesh(x, y, amplitudes_logged)
            plt.show()

        :param start_s: The start time. Starts at the beginning if neither this nor `start_sample` is specified.
        :param duration_s: The duration of the spectrogram in seconds. Goes to the end if neither this nor
                           `num_samples` is specified.
        :param start_sample: The index of the first sample to use. Starts at the beginning if neither this nor
                             `start_s` is specified.
        :param num_samples: The number of samples in the spectrogram. Goes to the end if neither this nor
                            `duration_s` is specified.
        :param window_length_s: The length of each FFT in seconds. If the total number of samples in the spectrogram
                                is not a multiple of the window length in samples, the last window will be zero-padded.
        :param window_length_samples: The length of each FFT in number of samples. If the total number of samples in the
                                spectrogram is not a multiple of the window length in samples, the last window will
                                be zero-padded.
        :param overlap: The fraction of each window to overlap.
        :returns: Three np.ndarrays: The frequency values in Hz (the y-axis in a spectrogram), the time values starting
                  at start time and then increasing by `duration_s` each step (the x-axis in a spectrogram), and
                  the dB of each time/frequency bin as a 2D array of shape [len(frequency values), len(duration)].
        :raises ValueError: If `start_s` and `start_sample` are both specified, if `duration_s` and `num_samples` are both
                            specified, if the first window's duration plus start time lead to running off the end
                            of the AudioSegment, or if `window_length_s` and `window_length_samples` are either
                            both specified or if they are both not specified.
        """
        if start_s is not None and start_sample is not None:
            raise ValueError("Only one of start_s and start_sample may be specified.")
        if duration_s is not None and num_samples is not None:
            raise ValueError("Only one of duration_s and num_samples may be specified.")
        if window_length_s is not None and window_length_samples is not None:
            raise ValueError("Only one of window_length_s and window_length_samples may be specified.")
        if window_length_s is None and window_length_samples is None:
            raise ValueError("You must specify a window length, either in window_length_s or in window_length_samples.")

        if start_s is None and start_sample is None:
            start_sample = 0
        if duration_s is None and num_samples is None:
            num_samples = len(self.get_array_of_samples()) - int(start_sample)

        if duration_s is not None:
            num_samples = int(round(duration_s * self.frame_rate))
        if start_s is not None:
            start_sample = int(round(start_s * self.frame_rate))

        if window_length_s is not None:
            window_length_samples = int(round(window_length_s * self.frame_rate))

        if start_sample + num_samples > len(self.get_array_of_samples()):
            raise ValueError("The combination of start and duration will run off the end of the AudioSegment object.")

        starts = []
        next_start = start_sample
        while next_start < len(self.get_array_of_samples()):
            starts.append(next_start)
            next_start = next_start + int(round(overlap * window_length_samples))

        rets = [self.fft(start_sample=start, num_samples=window_length_samples, zero_pad=True) for start in starts]
        bins = rets[0][0]
        values = [ret[1] for ret in rets]
        times = [start_sample / self.frame_rate for start_sample in starts]
        return np.array(bins), np.array(times), np.array(values)

    def trim_to_minutes(self, strip_last_seconds=False):
        """
        Returns a list of minute-long (at most) Segment objects.

        .. note:: I will likely depricate this method at some point. I have used it for a specific purpose, but
                  now we can just use the dice function.

        :param strip_last_seconds: If True, this method will return minute-long segments,
                                   but the last three seconds of this AudioSegment won't be returned.
                                   This is useful for removing the microphone artifact at the end of the recording.
        :returns: A list of AudioSegment objects, each of which is one minute long at most
                  (and only the last one - if any - will be less than one minute).
        """
        outs = self.dice(seconds=60, zero_pad=False)

        # Now cut out the last three seconds of the last item in outs (it will just be microphone artifact)
        # or, if the last item is less than three seconds, just get rid of it
        if strip_last_seconds:
            if outs[-1].duration_seconds > 3:
                outs[-1] = outs[-1][:-MS_PER_S * 3]
            else:
                outs = outs[:-1]

        return outs

    def zero_extend(self, duration_s=None, num_samples=None):
        """
        Adds a number of zeros (digital silence) to the AudioSegment (returning a new one).

        :param duration_s: The number of seconds of zeros to add. If this is specified, `num_samples` must be None.
        :param num_samples: The number of zeros to add. If this is specified, `duration_s` must be None.
        :returns: A new AudioSegment object that has been zero extended.
        :raises: ValueError if duration_s and num_samples are both specified.
        """
        if duration_s is not None and num_samples is not None:
            raise ValueError("`duration_s` and `num_samples` cannot both be specified.")
        elif duration_s is not None:
            num_samples = self.frame_rate * duration_s
        seg = AudioSegment(self.seg, self.name)
        zeros = silent(duration=num_samples / self.frame_rate, frame_rate=self.frame_rate)
        return zeros.overlay(seg)

def empty():
    """
    Creates a zero-duration AudioSegment object.

    :returns: An empty AudioSegment object.
    """
    dubseg = pydub.AudioSegment.empty()
    return AudioSegment(dubseg, "")

def from_file(path):
    """
    Returns an AudioSegment object from the given file based on its file extension.
    If the extension is wrong, this will throw some sort of error.

    :param path: The path to the file, including the file extension.
    :returns: An AudioSegment instance from the file.
    """
    _name, ext = os.path.splitext(path)
    ext = ext.lower()[1:]
    seg = pydub.AudioSegment.from_file(path, ext)
    return AudioSegment(seg, path)

def from_mono_audiosegments(*args):
    """
    Creates a multi-channel AudioSegment out of multiple mono AudioSegments (two or more). Each mono
    AudioSegment passed in should be exactly the same number of samples.

    :returns: An AudioSegment of multiple channels formed from the given mono AudioSegments.
    """
    return AudioSegment(pydub.AudioSegment.from_mono_audiosegments(*args), "")

def silent(duration=1000, frame_rate=11025):
    """
    Creates an AudioSegment object of the specified duration/frame_rate filled with digital silence.

    :param duration: The duration of the returned object in ms.
    :param frame_rate: The samples per second of the returned object.
    :returns: AudioSegment object filled with pure digital silence.
    """
    seg = pydub.AudioSegment.silent(duration=duration, frame_rate=frame_rate)
    return AudioSegment(seg, "")

# Tests
if __name__ == "__main__":
    #Uncomment to test
    #import matplotlib.pyplot as plt

    if len(sys.argv) != 2:
        print("For testing this module, USAGE:", sys.argv[0], os.sep.join("path to wave file.wav".split(' ')))
        exit(1)

    print("Reading in the wave file...")
    seg = from_file(sys.argv[1])

    print("Information:")
    print("Channels:", seg.channels)
    print("Bits per sample:", seg.sample_width * 8)
    print("Sampling frequency:", seg.frame_rate)
    print("Length:", seg.duration_seconds, "seconds")

    print("Resampling to 32kHz, mono, 16-bit...")
    seg = seg.resample(sample_rate_Hz=32000, sample_width=2, channels=1)

    print("Trimming to 30 ms slices...")
    slices = seg.dice(seconds=0.03, zero_pad=True)
    print("  |-> Got", len(slices), "slices.")
    print("  |-> Durations in seconds of each slice:", [sl.duration_seconds for sl in slices])

    print("Doing FFT and plotting the histogram...")
    print("  |-> Computing the FFT...")
    hist_bins, hist_vals = seg[1:3000].fft()
    hist_vals = np.abs(hist_vals) / len(hist_vals)
    print("  |-> Plotting...")
#    hist_vals = 10 * np.log10(hist_vals + 1e-9)
    plt.plot(hist_bins / 1000, hist_vals)#, linewidth=0.02)
    plt.xlabel("kHz")
    plt.ylabel("dB")
    plt.show()

    print("Doing a spectrogram...")
    print("  |-> Computing overlapping FFTs...")
    hist_bins, times, amplitudes = seg[1:3000].spectrogram(window_length_s=0.03, overlap=0.5)
    hist_bins = hist_bins / 1000
    amplitudes = np.abs(amplitudes) / len(amplitudes)
    amplitudes = 10 * np.log10(amplitudes + 1e-9)
    print("  |-> Plotting...")
    x, y = np.mgrid[:len(times), :len(hist_bins)]
    fig, ax = plt.subplots()
    ax.pcolormesh(x, y, amplitudes)
    plt.show()

    print("Detecting voice...")
    results = seg.detect_voice(prob_detect_voice=0.7)
    voiced = [tup[1] for tup in results if tup[0] == 'v']
    unvoiced = [tup[1] for tup in results if tup[0] == 'u']
    print("  |-> reducing voiced segments to a single wav file 'voiced.wav'")
    if len(voiced) > 1:
        voiced_segment = voiced[0].reduce(voiced[1:])
    elif len(voiced) > 0:
        voiced_segment = voiced[0]
    else:
        voiced_segment = None
    if voiced_segment is not None:
        voiced_segment.export("voiced.wav", format="WAV")
    print("  |-> reducing unvoiced segments to a single wav file 'unvoiced.wav'")
    if len(unvoiced) > 1:
        unvoiced_segment = unvoiced[0].reduce(unvoiced[1:])
    elif len(unvoiced) > 0:
        unvoiced_segment = unvoiced[0]
    else:
        unvoiced_segment = None
    if unvoiced_segment is not None:
        unvoiced_segment.export("unvoiced.wav", format="WAV")

    print("Splitting into frames...")
    segments = [s for s in seg.generate_frames_as_segments(frame_duration_ms=1000, zero_pad=True)]
    print("Got this many segments after splitting them up into one second frames:", len(segments))

    if voiced_segment is not None:
        print("Removing silence from voiced...")
        seg = voiced_segment.filter_silence()
        outname_silence = "nosilence.wav"
        seg.export(outname_silence, format="wav")
        print("After removal:", outname_silence)
