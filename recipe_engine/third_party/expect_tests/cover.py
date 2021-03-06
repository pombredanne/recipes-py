# Copyright 2014 The Chromium Authors. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

from cStringIO import StringIO
import os
import socket
import threading

import coverage

# This is instead of a contextmanager because it causes old pylints to crash :(
class _Cover(object):

  # Counter and associated lock for assigning unique IDs for coverage filename
  # suffixes. The coverage library's automatic suffix generation uses 6 digit
  # random integer and we were seeing collisions and files getting clobbered.
  current_id_lock = threading.Lock()
  current_id = 0

  @classmethod
  def unique_id(cls):
    """Returns a unique integer for this process."""
    with cls.current_id_lock:
      cls.current_id += 1
      return cls.current_id

  def __init__(self, enabled, maybe_kwargs):
    self.enabled = enabled
    self.kwargs = maybe_kwargs or {}
    self.c = None

  def __call__(self, **kwargs):
    new_kwargs = self.kwargs
    if self.enabled:
      new_kwargs = new_kwargs.copy()
      new_kwargs.update(kwargs)
    return _Cover(self.enabled, new_kwargs)

  def __enter__(self):
    if self.enabled:
      if self.c is None:
        kwargs = self.kwargs.copy()
        kwargs['data_suffix'] = "%s.%s.%s" % (
            socket.gethostname(), os.getpid(), self.unique_id())
        self.c = coverage.coverage(**kwargs)
        self.c._warn_no_data = False # pylint: disable=protected-access
      self.c.start()

  def __exit__(self, *_):
    if self.enabled:
      self.c.stop()
      self.c.save()


class CoverageContext(object):
  def __init__(self, name, cover_branches, html_report, enabled=True):
    self.opts = None
    self.cov = None
    self.enabled = enabled

    self.html_report = html_report

    if enabled:
      self.opts = {
        'data_file': '.%s_coverage' % name,
        'data_suffix': True,
        'branch': cover_branches,
      }
      self.cov = coverage.coverage(**self.opts)
      self.cov.erase()

  def cleanup(self):
    if self.enabled:
      self.cov.combine()

  def report(self, verbose, threshold, omit=None):
    fail = False

    if self.enabled:
      if self.html_report:
        self.cov.html_report(directory=self.html_report, omit=omit)

      outf = StringIO()

      try:
        coverage_percent = self.cov.report(file=outf, omit=omit)
      except coverage.CoverageException as ce:
        if ce.message != 'No data to report.':
          raise
        # If we have no data to report, this means that coverage has found no
        # tests in the module. Earlier version of coverage used to return 100 in
        # this case, so we simulate it to keep backward compatibility.
        coverage_percent = 100.0
        if verbose:
          print 'No data to report, setting coverage to 100%'

      fail = int(coverage_percent) != int(threshold)
      summary = outf.getvalue().replace('%- 15s' % 'Name', 'Coverage Report', 1)
      if verbose:
        print
        print summary
      elif fail:
        print
        lines = summary.splitlines()
        lines[2:-2] = [l for l in lines[2:-2]
                       if not l.strip().endswith('100%')]
        print '\n'.join(lines)
        print
        print ('FATAL: Test coverage %.f%% is not the required %.f%% threshold'
               % (int(coverage_percent), int(threshold)))

    return not fail

  def create_subprocess_context(self):
    # Can't have this method be the contextmanager because otherwise
    # self (and self.cov) will get pickled to the subprocess, and we don't want
    # that :(
    return _Cover(self.enabled, self.opts)
