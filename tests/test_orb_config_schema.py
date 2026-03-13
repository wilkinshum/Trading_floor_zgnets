import os
import json
import yaml
import unittest
import re


TIME_RE = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")


class TestOrbConfigSchema(unittest.TestCase):
    def setUp(self):
        base_dir = os.path.dirname(__file__)
        self.config_dir = os.path.join(base_dir, '..', 'configs')
        self.orb_config_path = os.path.join(self.config_dir, 'orb_config.yaml')

    def _load_yaml(self, path):
        with open(path, 'r', encoding='utf-8') as f:
            return yaml.safe_load(f)

    def _walk(self, obj, path=None):
        if path is None:
            path = []
        if isinstance(obj, dict):
            for k, v in obj.items():
                yield from self._walk(v, path + [str(k)])
        elif isinstance(obj, list):
            for i, v in enumerate(obj):
                yield from self._walk(v, path + [str(i)])
        else:
            yield path, obj

    def _to_minutes(self, time_str):
        h, m = time_str.split(':')
        return int(h) * 60 + int(m)

    def test_time_strings_format(self):
        data = self._load_yaml(self.orb_config_path)
        for path, value in self._walk(data):
            if isinstance(value, str):
                key = path[-1].lower() if path else ''
                if ':' in value or 'time' in key:
                    self.assertRegex(value, TIME_RE, msg=f"Invalid time format at {'.'.join(path)}: {value}")

    def test_percentage_values_between_zero_and_one(self):
        data = self._load_yaml(self.orb_config_path)
        for path, value in self._walk(data):
            if isinstance(value, (int, float)):
                key = path[-1].lower() if path else ''
                if 'pct' in key or 'percent' in key:
                    self.assertGreaterEqual(value, 0.0, msg=f"{'.'.join(path)} below 0")
                    self.assertLessEqual(value, 1.0, msg=f"{'.'.join(path)} above 1")

    def test_circuit_breaker_drawdown_monotonic(self):
        data = self._load_yaml(self.orb_config_path)
        breakers = data['orb']['risk']['circuit_breakers']
        drawdowns = [b['drawdown_pct'] for b in breakers]
        self.assertEqual(drawdowns, sorted(drawdowns), msg="Circuit breaker drawdown_pct not increasing")

    def test_target_scale_chronological(self):
        data = self._load_yaml(self.orb_config_path)
        exit_cfg = data['orb']['exit']
        target_scale = exit_cfg.get('target_scale')
        if target_scale:
            times = []
            for entry in target_scale:
                t = entry.get('time') or entry.get('at')
                if t:
                    times.append(t)
            if times:
                minutes = [self._to_minutes(t) for t in times]
                self.assertEqual(minutes, sorted(minutes), msg="target_scale entries not chronological")

    def test_no_duplicate_signal_names(self):
        data = self._load_yaml(self.orb_config_path)
        signals = data['orb']['self_learning']['signals']
        self.assertEqual(len(signals), len(set(signals)), msg="Duplicate signal names found")


if __name__ == '__main__':
    unittest.main()
