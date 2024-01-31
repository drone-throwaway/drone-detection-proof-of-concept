#!/usr/bin/env python3
#
# take input from hackrf_sweep, and listen for strong signals
# note: hackrf_sweep doesn't announce a version. It emits
# measurements as text (default) on stdout. stderr has info
# messages
#
# text output format of hackrf_sweep:
# date, time, hz_low, hz_high, hz_bin_width, num_samples, dB, dB, . . .
#
# note: this is measurements in [hz_low, hz_high) (high end not inclusive)
#
# do basic splits ourselves rather than do csv or something
#
# if this isn't performant enough, we'll need to do something besides python,
# or get clever...

import argparse
from multiprocessing import Process
import sys
import time

from hackrf_sweep_classes import HackSweepLine, Measurement, SignalStore

# estimate current environmental noise floor based on averages of buckets
# average_bucket_strengths: dict mapping min frequency of a bucket, to average observed signal strength
def compute_noise_floor(average_bucket_strengths):

	# just an estimate (not sure if there's a canonical algorithm for this)
	# take weakest 25% of signals, and average just those.
	sorted_dbs = sorted(average_bucket_strengths.values())
	quarter_len = len(sorted_dbs) // 4
	noise_floor = sum( sorted_dbs[:quarter_len] ) / quarter_len

	return noise_floor

# given signal strengths by bucket, and a noise floor, compute which buckets
# have a signal, and what that signal strength is
def find_signal_buckets(average_bucket_strengths, noise_floor):

	# dB above noise floor at which point we consider something a "signal"
	# experimentally, +3 db above noise floor in one "no drone" case got at most 7 'signal' buckets
	# who were barely above the noise floor (floor about -66, signals -63), while the "drone"
	# case successfully had the video downlink detected
	SQUELCH = 3

	# TODO: turn this into a dict?
	has_signal = []
	signal_strengths = []
	for k in sorted(average_bucket_strengths.keys()):
		if average_bucket_strengths[k] > noise_floor + SQUELCH:
			# has_signal is given signals sorted by frequency (and thus bucket)
			# indices match between has_signal and signal_strengths (in order with each other)
			# TODO: making this a dict will be nicer, since then we won't
			# have to be so careful in making sure the indices match up
			has_signal.append(k)
			signal_strengths.append(average_bucket_strengths[k])

	return (has_signal, signal_strengths)

# given a list of buckets which have signal, return a list of the frequency ranges
# which are contiguous
def get_contiguous_regions(bucket_width, bucket_ranges, has_signal, signal_strengths):
	# need to look for contiguous buckets with signal that are 10MHz or 20MHz wide (or wider?)
	# walk has_signal array and get full bucket ranges
	# NOTE: this assumes that has_signal is sorted
	signal_ranges = [ bucket_ranges[f] for f in has_signal ]
	# tally width of contiguous signal ranges
	contiguous_regions = [] # tuple of [min, max)
	i = 0
	if len(signal_ranges) > 2:
		while True:
			# initial contiguous region is this bucket (by definition)
			# (a single bucket on its own is contiguous with itself)
			start = signal_ranges[i][0]
			end = signal_ranges[i][1]

			# find the end of this contiguous region
			# ranges are [a, b), [c, d), so contiguous iff. b == c
			while signal_ranges[i][1] == signal_ranges[i+1][0]:
				end = signal_ranges[i+1][0]
				i += 1

				# end case (no more signals)
				if i >= len(signal_ranges) - 1:
					break

			# [start, end) now reflects a contiguous region
			contiguous_regions.append( (start, end) )
			i += 1

			# end case (no more signals)
			if i >= len(signal_ranges) - 1:
				break

	return contiguous_regions

# get the regions of contiguous signal which are wide enough to
# be considered drone video downlinks
def get_drone_regions(contiguous_signal_regions):
	BANDWIDTH_THRESHOLD = 10000 # 10MHz

	drone_regions = []
	for r in contiguous_signal_regions:
		if r[1] - r[0] > BANDWIDTH_THRESHOLD:
			drone_regions.append(r)
	return drone_regions

# analyze range of measurements
# if there's a constant 10 MHz or 20 MHz bandwith signal, assume it's a drone.
# If not, assume no drone
# ranges: dict mapping lowest frequency of bucket to list of measurements
#
# rough detection heuristic: lowest signal strengths probably decent candidates
# for the noise floor. Take avg of lowest quarter of measurements?
# if we have a bucket whose measurement is some level of db above this (squelch)
# consider this a signal. If we have a contiguous 20 MHz or 10 MHz of buckets with
# a signal, consider this a drone video downlink.
#
# 
def analyze(signal_store):
	# TODO: do better
	now = time.time()
	
	if signal_store.config['skip_analysis']:
		print(f'[I] {now} skipping analysis...')
		return

	# get 'frozen' copy of data to analyze
	measurements = signal_store.get_measurements_copy()

	# map min frequency of bucket to average signal strength in dB
	averages = {}
	# go from lowest frequency to highest
	for f in sorted(measurements.keys()):
		ms = measurements[f]
		# compute and store the average signal strength for this frequency bucket
		# (smooths out signal spikes)
		if len(ms) != 0:
			avg_db = sum( [ m.db for m in ms ] ) / len(ms)
			averages[f] = avg_db
			#print(f"[I] {f = }: {avg_db}") # debug
	
	noise_floor = compute_noise_floor(averages)

	# find which frequency ranges have a signal
	(has_signal, signal_strengths) = find_signal_buckets(averages, noise_floor)
	
	# check for contiguous ranges of detected signals
	bucket_width = signal_store.get_bucket_width()
	# map min frequency of bucket to full range of bucket [min, max)
	# thus, contiguous iff i[max] == i+1[min]
	bucket_ranges = { f: (f, f + bucket_width) for f in averages.keys() }

	contiguous_regions = get_contiguous_regions(bucket_width, bucket_ranges, has_signal, signal_strengths)

	# see if any contiguous regions are wide enough to assume they're a video downlink
	drone_regions = get_drone_regions(contiguous_regions)
	drone_detected = False
	if drone_regions:
		drone_detected = True

	#print(f"[I] signal buckets {len(has_signal)}, {now = }, {noise_floor = :0.1f}, {has_signal = }, {signal_strengths = }, {signal_ranges = }, {contiguous_regions = }") # debug
	print(f"[I] {drone_detected = }")

# read hackrf_sweep output from a file
# kick off periodic analyzing by line count
# for debugging
def handle_file(filename):
	raise NotImplementedError("use replay_capture.py & stdin interface")

# take hackrf_sweep lines from stdin directly
def handle_input(store):
	# maps frequency bucket (indexed by lowest frequency) to list of measurements
	# janky attempt to analyze once a second
	last_datetime = None
	while True:
		line = sys.stdin.readline()
		if not line:
			break

		# parse measurements & add to our measurements buffer
		hsl = HackSweepLine(line)
		measurements = hsl.to_measurements()
		store.add_measurements(measurements)
		
		if not last_datetime:
			last_datetime = hsl.datetime

		# do drone check once a second
		# (hackrf_sweep has 1s granularity on timestamps)
		if hsl.datetime != last_datetime:
			# TODO: would be nice to have a longer-lived analysis
			# process to avoid startup cost of a new process for
			# each analysis
			last_datetime = hsl.datetime
			#print(f'[I] running analysis for {hsl.datetime}') # debug
			if store.is_warmed_up():
				Process(target=analyze, args=(store,)).start()
	pass

if __name__ == '__main__':
	# note: over the time of hackrf_sweep running, it won't change configuration
	# so, frequency range being scanned, # of buckets won't change. This will
	# enable comparisons over time of each bucket
	#
	# we can also do a sanity check of first measurements to make sure we're
	# scanning the desired range correctly
	#
	# for a DJI Mavic Pro 1 drone, want to find a 10MHz or 20MHz bandwidth
	# signal in 2.4035 GHz to 2.4775 GHz. This appears to be the video downlink
	#
	# expected input: `hackrf_sweep -f 2403:2478`

	parser = argparse.ArgumentParser(description='listen to hackrf_sweep output for drone video downlink signals')
	parser.add_argument('-f', '--file', help='a file to read data from (from hackrf_sweep > file')
	parser.add_argument('--skip-analysis', action='store_const', const=True, help='skip analysis to benchmark parsing & data storing/retrieval')

	args = parser.parse_args()

	cfg = {'skip_analysis': args.skip_analysis}
	store = SignalStore(cfg)
	if args.file:
		# read input from file (mostly for debugging)
		handle_file(args.file)
	else:
		# read input from stdin (assume live hackrf_sweep instance)
		handle_input(store)
