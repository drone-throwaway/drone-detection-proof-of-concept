#!/usr/bin/env python3

# lock intended to use for analysis in a separate process
# (want a proper thread/process-safe lock)
from multiprocessing import Lock

# to deeply copy measurements from ringbufs
from copy import deepcopy # not used?

import datetime

# goal is to be a container for our active-use signal measurements,
# as well as holder of 'global' config & state to enable analysis
# and monitoring
class SignalStore:

	# purpose-built class to help us have a ring buffer.
	# a ring buffer fills up with data, then when we reach the end
	# and it's full, we wrap back around to the beginning and overwrite
	# the oldest data.
	# You shouldn't have to touch this.
	class _ringbuf:

		# size: size in elements of ringbuf
		# 	we want this large enough to contain a full second of data
		def __init__(self, size):
			self._size = size
			self._cursor = 0
			# TODO: could probably replace with a faster structure
			self._data = []
			self._full = False
			self._lock = Lock()

		def add(self, val):
			with self._lock:
				if self._full:
					self._data[self._cursor] = val
					self._cursor = (self._cursor + 1) % len(self._data)
				else:
					self._data.append(val)
					if len(self._data) >= self._size:
						self._full = True

		def get_data_copy(self):
			with self._lock:
				if self._full:
					# slices make shallow copies
					data = self._data[self._cursor:] + self._data[:self._cursor]
					# proper deep copy so data can't be changed
					# and mix up analyzing or other users
					# (unnecessary if using basic types)
					#return deepcopy(data)
					return data
				else:
					return self._data.copy()
		# unused
		def is_full(self):
			return self._full

		# removed dead code: oldest entry at 0 or self._cursor,
		# newest entry at -1 or self._cursor - 1

	# config: opaque object to store global config (bad design)
	def __init__(self, config):
		# map lowest frequency of bucket to ringbuf of most recent measurements

		# Want to guarantee that we have ~1s of measurements in ringbuf,
		# realistically between 1 and 2 seconds worth, since granularity
		# of given measurements is one second
		# do a warmup period of measurements. Pick a frequency bucket
		# (first one we see) and count the number of measurements we get
		# for it to elapse one entire second. Then set our ringbufs to that
		# size (which requires remapping ringbufs...)
		# TODO: do health checks based on this to see
		# 1) if we aren't getting enough new measurements
		# 2) if we're getting too many new measurements

		# TODO: can have a separate ringbuf for each second. Use current
		# ringbuf (possibly incomplete) and most recent (at least 1s worth)
		# while tossing older ringbufs
		self._ranges = {}

		self._BUFSIZE = 200
		self._bucket_width = None
		self.config = config

		self._warmed_up = False
		self._warmup_measurement_count = 0
		self._warmup_bucket_frequency = 0
		self._warmup_first_datetime = 0
		self._warmup_resize_ringbufs = False

	def is_warmed_up(self):
		return self._warmed_up

	def get_bucket_width(self):
		if self._bucket_width:
			return self._bucket_width
		else:
			raise Exception("no measurements yet, unknown bucket width!")
	
	# get 'snapshot' copy of signals for analysis without blocking further parsing
	def get_measurements_copy(self):
		copy = {}

		for k in self._ranges.keys():
			copy[k] = self._ranges[k].get_data_copy()

		return copy

	# given list of Measurement objects, add them to our
	# ringbufs of measured signals.
	# This includes warmup logic, which we use to dynamically resize our
	# ringbufs to hold approximately 1 second of measurements. This way
	# our whole analysis process only focuses on the most recent second
	# of signals.
	# measurements: list of Measurement objects
	def add_measurements(self, measurements):
		# if we haven't observed a bucket size yet, note it
		# (it should be fixed for all buckets, over hackrf_sweep's runtime)
		if not self._bucket_width:
			m = measurements[0]
			self._bucket_width = m.hz_high - m.hz_low

		# pick arbitrary bucket to watch for timing the warmup process
		# (first bucket)
		if not self._warmup_bucket_frequency:
			self._warmup_bucket_frequency = measurements[0].hz_low
			self._warmup_first_datetime = measurements[0].datetime

		# check for resize signal & complete warmup process
		if not self.is_warmed_up() and self._warmup_resize_ringbufs:
			self._warmup_resize_ringbufs = False
			# resize
			print(f"[I] (warmup) resizing ringbufs to {self._warmup_measurement_count} elements")
			self._BUFSIZE = self._warmup_measurement_count
			# race condition here, but add_measurement is only ever
			# called from single thread right now, so safe. All ringbufs
			# will be destroyed before another measurement is added
			tmpkeys = list(self._ranges.keys())
			for k in tmpkeys:
				del(self._ranges[k])
			# let usual add process create new ringbuf objects
			self._warmed_up = True

		one_second = datetime.timedelta(seconds=1)
		for m in measurements:
			# warmup logic, for dynamically resizing ringbuf to have
			# roughly 1s of measurements
			if not self.is_warmed_up() and \
				self._warmup_bucket_frequency == m.hz_low:
				self._warmup_measurement_count += 1

				# if first time more than one second has elapsed
				# set flag to signal for resize process to
				# complete warmup
				if m.datetime - self._warmup_first_datetime > one_second:
					self._warmup_resize_ringbufs = True

			# typical case
			try:
				self._ranges[m.hz_low].add(m)
			except KeyError:
				# haven't seen this bucket yet; make ringbuf (first time)
				self._ranges[m.hz_low] = self._ringbuf(self._BUFSIZE)
				self._ranges[m.hz_low].add(m)


# Parse a line of output from hackrf_sweep, produce measurements from it
class HackSweepLine:
	def __init__(self, line):
		fields = line.split(',')

		# TODO: using datetime types would be nice
		tmp_date = fields.pop(0)
		tmp_time = fields.pop(0)
		tmp_datetime = tmp_date + ' ' + tmp_time
		self.datetime = datetime.datetime.strptime(tmp_datetime, '%Y-%m-%d %H:%M:%S')
		self.hz_low = int(fields.pop(0))
		self.hz_high = int(fields.pop(0))
		self.hz_bin_width = float(fields.pop(0))
		self.num_samples = int(fields.pop(0))
		self.samples = [float(db) for db in fields]

	# produce a measurement of each bucket in this line
	# returns a list of measurements (arbitrary length)
	def to_measurements(self):
		# NOTE: this should always be a legitimate int, but it would be nice
		# to tell if it isn't
		num_buckets = int( (self.hz_high - self.hz_low) / self.hz_bin_width )
		hz_bin_width = int(self.hz_bin_width)

		if hz_bin_width != self.hz_bin_width:
			raise Exception("non-integer bin width for measurements")

		# get each bucket's measurement 
		i = 0
		measurements = []
		for f in range(self.hz_low, self.hz_high, hz_bin_width):
			m = Measurement(self.datetime, f, f + hz_bin_width, self.samples[i])
			i += 1
			measurements.append(m)

		return measurements

class Measurement:
	def __init__(self, datetime, hz_low, hz_high, db):
		self.datetime = datetime
		self.hz_low = hz_low
		self.hz_high = hz_high
		self.db = db

if __name__ == '__main__':
	# this should only be imported
	raise Exception("hackrf_sweep_classes should only be imported")
	pass
