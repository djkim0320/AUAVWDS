
import unittest
import os, sys

curr_path = os.path.dirname(os.path.realpath(__file__))
vsp_path = os.path.join(curr_path, '../..')
sys.path.insert(1, vsp_path)

from openvsp import *

class TestOpenVSP(unittest.TestCase):
	def setUp(self):
		VSPRenew()
	def test_IsFacade(self):
		is_facade = IsFacade()
	def test_IsGUIRunning(self):
		is_gui_active = IsGUIRunning()

if __name__ == '__main__':
    unittest.main()
