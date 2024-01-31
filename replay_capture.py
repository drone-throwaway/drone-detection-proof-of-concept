#!/usr/bin/env python3
#
# given a text file logged from hackrf_sweep, "replay"
# it to simulate a live-running hackrf_sweep instance

import argparse
import time

from hackrf_sweep_classes import HackSweepLine

# 2nd field is time (hh:mm:ss)
def get_time(line):
	fields = line.split(',')
	return fields[1]

if __name__ == '__main__':
	# dict mapping time -> list of lines
	lines_by_second = {}

	parser = argparse.ArgumentParser(description='janky replay of hackrf_sweep logs to simulate a live hackrf_sweep')
	parser.add_argument('file', help='hackrf_sweep logfile to replay')

	args = parser.parse_args()

	# group lines by time (expected format: hh:mm:ss)
	with open(args.file, 'r') as f:
		for l in f:
			t = get_time(l)
			try:
				lines_by_second[t].append(l)
			except KeyError:
				lines_by_second[t] = []

	# output each second's lines, in the order indicated by timestamps.
	for k in sorted(lines_by_second.keys()):
		for l in lines_by_second[k]:
			print(l.rstrip())
		time.sleep(1)
