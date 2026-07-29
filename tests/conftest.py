# -*- coding: utf-8 -*-
"""Put src/ on sys.path so tests can import the application modules directly
(`import analysis`), regardless of the working directory pytest is run from."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))
