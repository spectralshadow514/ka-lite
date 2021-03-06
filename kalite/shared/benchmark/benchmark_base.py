""" base class for KA-LITE benchmarks

    Provides a wrapper for benchmarks, specifically to:
    a) accurately record the test conditions/environment
    b) to record the duration of the task
    d) to allow multiple iterations of the task so that
        an average time can be calculated
        
    Benchmark results are returned in a python dict

    These benchmarks do not use unittest or testrunner frameworks
"""
import time
import random
import platform
import subprocess
from selenium import webdriver

from django.core import management

from settings import LOG as logging


class Common(object):

    def __init__(self, comment=None, fixture=None, **kwargs):

        self.return_dict = {}
        self.return_dict['comment'] = comment
        self.return_dict['class']=type(self).__name__
        self.return_dict['uname'] = platform.uname()
        self.return_dict['fixture'] = fixture
                                
        try:
            branch = subprocess.Popen(["git", "describe", "--contains", "--all", "HEAD"], stdout=subprocess.PIPE).communicate()[0]
            self.return_dict['branch'] = branch[:-1]
            head = subprocess.Popen(["git", "log", "--pretty=oneline", "--abbrev-commit", "--max-count=1"], stdout=subprocess.PIPE).communicate()[0]
            self.return_dict['head'] = head[:-1]
        except:
            self.return_dict['branch'] = None
            self.return_dict['head'] = None            

        # if setup fails, what could we do?
        try:
            self._setup(**kwargs)
        except Exception as e:
            logging.error(e)

    def execute(self, iterations=1):

        if iterations < 1: iterations = 1

        if hasattr(self, 'max_iterations'):
            if iterations > self.max_iterations:
                iterations = self.max_iterations
        
        self.return_dict['iterations'] = iterations
        self.return_dict['individual_elapsed'] = {}
        self.return_dict['post_execute_info'] = {}
        self.return_dict['exceptions'] = {}

        for i in range(iterations):
            self.return_dict['exceptions'][i+1] = []
            start_time = time.time()
            try:
                self._execute()
                self.return_dict['individual_elapsed'][i+1] = time.time() - start_time
            except Exception as e:
                self.return_dict['individual_elapsed'][i+1] = None
                self.return_dict['exceptions'][i+1].append(e)

            try:
                self.return_dict['post_execute_info'][i+1] = self._get_post_execute_info()
            except Exception as e:
                self.return_dict['post_execute_info'][i+1] = None
                self.return_dict['exceptions'][i+1].append(e)



        mean = lambda vals: sum(vals)/float(len(vals)) if len(vals) else None
        self.return_dict['average_elapsed'] = mean([v for v in self.return_dict['individual_elapsed'].values() if v is not None])

        try:
            self._teardown()
        except Exception as e:
            logging.error(e)

        return self.return_dict

    def _setup(self, behavior_profile=None, **kwargs):
        """
        All benchmarks can take a random seed,
        all should clean / recompile
        """
        if behavior_profile:
            self.behavior_profile = behavior_profile
            random.seed(self.behavior_profile)
        management.call_command('clean_pyc')
        management.call_command('compile_pyc')

    def _execute(self): pass
    def _teardown(self): pass
    def _get_post_execute_info(self): pass


class SeleniumCommon(Common):
    def _setup(self, url="http://localhost:8008/", username="s1", password="s1", timeout=30, **kwargs):
        # Note: user must exist
        super(SeleniumCommon, self)._setup(**kwargs)

        self.url = url
        self.username = username
        self.password = password

        self.browser = webdriver.Firefox()
        self.timeout = timeout

    def _teardown(self):
        self.browser.close()
