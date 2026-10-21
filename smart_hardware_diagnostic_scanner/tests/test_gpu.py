"""Tests for GPU detection: missing tool, timeouts, failures, bad output."""
from __future__ import annotations

import subprocess
import unittest
from unittest.mock import patch

from scanner import gpu as g


class TestGpu(unittest.TestCase):
    def test_missing_nvidia_smi(self):
        with patch.object(g, "which", return_value=None):
            st = g.query_nvidia_gpus()
            self.assertFalse(st.supported)
            self.assertFalse(st.tool_found)
            self.assertEqual(st.gpus, [])
            self.assertIn("not found", st.message)

    def test_timeout(self):
        with patch.object(g, "which", return_value="nvidia-smi"), \
             patch.object(g.subprocess, "run",
                          side_effect=subprocess.TimeoutExpired(cmd="x", timeout=8)):
            st = g.query_nvidia_gpus(timeout=1)
            self.assertFalse(st.supported)
            self.assertIn("timed out", st.message)

    def test_nonzero_exit(self):
        fake = subprocess.CompletedProcess(args=["nvidia-smi"], returncode=1,
                                           stdout="", stderr="driver error")
        with patch.object(g, "which", return_value="nvidia-smi"), \
             patch.object(g.subprocess, "run", return_value=fake):
            st = g.query_nvidia_gpus()
            self.assertFalse(st.supported)
            self.assertIn("failed", st.message)

    def test_empty_output(self):
        fake = subprocess.CompletedProcess(args=["nvidia-smi"], returncode=0,
                                           stdout="\n", stderr="")
        with patch.object(g, "which", return_value="nvidia-smi"), \
             patch.object(g.subprocess, "run", return_value=fake):
            st = g.query_nvidia_gpus()
            self.assertFalse(st.supported)

    def test_valid_output_parsed(self):
        out = "0, NVIDIA GeForce RTX 3060, 12, 55, 1024, 8192, 30, 80\n"
        fake = subprocess.CompletedProcess(args=["nvidia-smi"], returncode=0,
                                           stdout=out, stderr="")
        with patch.object(g, "which", return_value="nvidia-smi"), \
             patch.object(g.subprocess, "run", return_value=fake) as m:
            st = g.query_nvidia_gpus()
            self.assertTrue(st.supported)
            self.assertEqual(len(st.gpus), 1)
            gpu = st.gpus[0]
            self.assertEqual(gpu.name, "NVIDIA GeForce RTX 3060")
            self.assertEqual(gpu.utilization_pct, 12.0)
            self.assertEqual(gpu.temperature_c, 55.0)
            # shell=False and argv list enforced
            _, kwargs = m.call_args
            self.assertFalse(kwargs.get("shell", True) is True)
            args, _ = m.call_args
            self.assertIsInstance(args[0], list)

    def test_na_values_become_none(self):
        out = "0, Some GPU, N/A, N/A, N/A, N/A, N/A, N/A\n"
        fake = subprocess.CompletedProcess(args=["nvidia-smi"], returncode=0,
                                           stdout=out, stderr="")
        with patch.object(g, "which", return_value="nvidia-smi"), \
             patch.object(g.subprocess, "run", return_value=fake):
            st = g.query_nvidia_gpus()
            self.assertTrue(st.supported)
            self.assertIsNone(st.gpus[0].temperature_c)
            self.assertIsNone(st.gpus[0].utilization_pct)

    def test_oserror_handled(self):
        with patch.object(g, "which", return_value="nvidia-smi"), \
             patch.object(g.subprocess, "run", side_effect=OSError("denied")):
            st = g.query_nvidia_gpus()
            self.assertFalse(st.supported)


class TestRocmProvider(unittest.TestCase):
    def test_rocm_missing_tool(self):
        with patch.object(g, "which", return_value=None):
            st = g.query_rocm_gpus()
        self.assertFalse(st.supported)
        self.assertIn("not found", st.message)

    def test_rocm_json_parsed(self):
        out = ('{"card0": {"GPU use (%)": "12", "Temperature (Sensor edge) (C)": '
               '"43.0", "VRAM Total Used Memory (B)": "52428800"},\n'
               ' "card1": {"GPU use (%)": "3", "Temperature (Sensor junction) (C)": '
               '"51.0"}}')
        fake = subprocess.CompletedProcess(args=["rocm-smi"], returncode=0,
                                           stdout=out, stderr="")
        with patch.object(g, "which", return_value="rocm-smi"), \
             patch.object(g.subprocess, "run", return_value=fake) as m:
            st = g.query_rocm_gpus()
        self.assertTrue(st.supported)
        self.assertEqual(len(st.gpus), 2)
        first = st.gpus[0]
        self.assertEqual(first.index, 0)
        self.assertEqual(first.utilization_pct, 12.0)
        self.assertEqual(first.temperature_c, 43.0)
        self.assertAlmostEqual(first.memory_used_mb, 50.0)
        _, kwargs = m.call_args
        self.assertFalse(kwargs.get("shell", True) is True)

    def test_rocm_bad_json_degrades(self):
        fake = subprocess.CompletedProcess(args=["rocm-smi"], returncode=0,
                                           stdout="not json", stderr="")
        with patch.object(g, "which", return_value="rocm-smi"), \
             patch.object(g.subprocess, "run", return_value=fake):
            st = g.query_rocm_gpus()
        self.assertFalse(st.supported)
        self.assertIn("JSON", st.message)

    def test_rocm_nonzero_exit(self):
        fake = subprocess.CompletedProcess(args=["rocm-smi"], returncode=2,
                                           stdout="", stderr="no driver")
        with patch.object(g, "which", return_value="rocm-smi"), \
             patch.object(g.subprocess, "run", return_value=fake):
            st = g.query_rocm_gpus()
        self.assertFalse(st.supported)
        self.assertIn("failed", st.message)


class TestIntelProvider(unittest.TestCase):
    def test_intel_missing_tool(self):
        with patch.object(g, "which", return_value=None):
            st = g.query_intel_gpus()
        self.assertFalse(st.supported)
        self.assertIn("not found", st.message)

    def test_intel_timeout_with_partial_output(self):
        # intel_gpu_top streams; a timeout is expected and partial JSON parses.
        exc = subprocess.TimeoutExpired(cmd="intel_gpu_top", timeout=3,
                                        output='{"gpu": {"busy": "27.5"}}\n')
        with patch.object(g, "which", return_value="intel_gpu_top"), \
             patch.object(g.subprocess, "run", side_effect=exc):
            st = g.query_intel_gpus()
        self.assertTrue(st.supported)
        self.assertEqual(st.gpus[0].utilization_pct, 27.5)

    def test_intel_timeout_without_output_degrades(self):
        exc = subprocess.TimeoutExpired(cmd="intel_gpu_top", timeout=3, output="")
        with patch.object(g, "which", return_value="intel_gpu_top"), \
             patch.object(g.subprocess, "run", side_effect=exc):
            st = g.query_intel_gpus()
        self.assertFalse(st.supported)

    def test_intel_nonzero_exit(self):
        fake = subprocess.CompletedProcess(args=["intel_gpu_top"], returncode=1,
                                           stdout="", stderr="no permission")
        with patch.object(g, "which", return_value="intel_gpu_top"), \
             patch.object(g.subprocess, "run", return_value=fake):
            st = g.query_intel_gpus()
        self.assertFalse(st.supported)
        self.assertIn("failed", st.message)


class TestGpuChain(unittest.TestCase):
    def test_chain_returns_first_supported(self):
        rocm_ok = g.GpuStatus(supported=True, tool_found=True, message="amd ok",
                              gpus=[g.GpuInfo(index=0, name="AMD", available=True)])
        with patch.object(g, "query_nvidia_gpus",
                          return_value=g.GpuStatus(message="no nvidia")), \
             patch.object(g, "query_rocm_gpus", return_value=rocm_ok), \
             patch.object(g, "query_intel_gpus") as never_intel:
            st = g.query_gpus()
        self.assertTrue(st.supported)
        never_intel.assert_not_called()

    def test_chain_lists_everything_tried_when_none_supported(self):
        with patch.object(g, "query_nvidia_gpus",
                          return_value=g.GpuStatus(tool_found=False,
                                                   message="nvidia: not found")), \
             patch.object(g, "query_rocm_gpus",
                          return_value=g.GpuStatus(tool_found=False,
                                                   message="rocm: not found")), \
             patch.object(g, "query_intel_gpus",
                          return_value=g.GpuStatus(tool_found=False,
                                                   message="intel: not found")):
            st = g.query_gpus()
        self.assertFalse(st.supported)
        self.assertIn("nvidia-smi", st.message)
        self.assertIn("rocm-smi", st.message)
        self.assertIn("intel_gpu_top", st.message)
        self.assertTrue(st.message.startswith("No supported GPU metrics"))

    def test_chain_survives_a_crashing_probe(self):
        with patch.object(g, "query_nvidia_gpus", side_effect=RuntimeError("boom")), \
             patch.object(g, "query_rocm_gpus",
                          return_value=g.GpuStatus(message="rocm: none")), \
             patch.object(g, "query_intel_gpus",
                          return_value=g.GpuStatus(message="intel: none")):
            st = g.query_gpus()
        self.assertFalse(st.supported)
        self.assertIn("error: boom", st.message)

    def test_hardware_snapshot_uses_the_chain(self):
        from scanner import hardware as hw
        self.assertTrue(hasattr(hw, "query_gpus"))


if __name__ == "__main__":
    unittest.main()
