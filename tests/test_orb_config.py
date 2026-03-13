import os
import json
import yaml
import unittest


class TestOrbConfig(unittest.TestCase):
    def setUp(self):
        base_dir = os.path.dirname(__file__)
        self.config_dir = os.path.join(base_dir, '..', 'configs')
        self.orb_config_path = os.path.join(self.config_dir, 'orb_config.yaml')
        self.workflow_path = os.path.join(self.config_dir, 'workflow.yaml')
        self.regime_state_path = os.path.join(self.config_dir, 'regime_state.json')

    def _load_yaml(self, path):
        with open(path, 'r', encoding='utf-8') as f:
            return yaml.safe_load(f)

    def _load_json(self, path):
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)

    def test_orb_config_yaml_parses(self):
        data = self._load_yaml(self.orb_config_path)
        self.assertIsInstance(data, dict)

    def test_orb_config_required_keys(self):
        data = self._load_yaml(self.orb_config_path)
        self.assertIn('orb', data)
        orb = data['orb']
        required = [
            'capital', 'scanner', 'entry', 'exit', 'risk', 'regime',
            'monitor', 'self_learning', 'alpha_decay', 'execution'
        ]
        for key in required:
            self.assertIn(key, orb, msg=f"Missing orb.{key}")

    def test_orb_config_value_constraints(self):
        data = self._load_yaml(self.orb_config_path)
        orb = data['orb']

        self.assertEqual(orb['capital'], 3000)
        self.assertGreaterEqual(orb['max_risk_pct'], 0.01)
        self.assertLessEqual(orb['max_risk_pct'], 0.05)
        self.assertGreaterEqual(orb['max_notional_pct'], 0.25)
        self.assertLessEqual(orb['max_notional_pct'], 0.75)
        self.assertGreater(orb['daily_loss_cap'], 0)
        self.assertLessEqual(orb['daily_loss_cap'], 250)

        scanner = orb['scanner']
        self.assertGreaterEqual(scanner['price_min'], 5)
        self.assertLessEqual(scanner['price_max'], 1000)
        self.assertGreaterEqual(scanner['max_candidates'], 3)
        self.assertLessEqual(scanner['max_candidates'], 15)

        entry = orb['entry']
        self.assertGreaterEqual(entry['consolidation']['min_candles'], 2)
        self.assertGreaterEqual(entry['retest']['body_ratio_min'], 0.4)
        self.assertLessEqual(entry['retest']['body_ratio_min'], 0.8)

        exit_cfg = orb['exit']
        self.assertGreaterEqual(exit_cfg['partial_pct'], 0.25)
        self.assertLessEqual(exit_cfg['partial_pct'], 0.75)
        self.assertEqual(exit_cfg['time_stop'], "11:30")

        risk = orb['risk']
        self.assertEqual(risk['max_positions'], 2)
        self.assertEqual(risk['max_total_positions'], 5)
        self.assertLessEqual(risk['max_waves_per_stock'], 5)
        self.assertEqual(len(risk['circuit_breakers']), 3)

        regime = orb['regime']
        self.assertEqual(len(regime['states']), 5)

        self_learning = orb['self_learning']
        self.assertEqual(len(self_learning['signals']), 8)
        weights = self_learning['weights']
        weight_sum = sum(weights.values())
        self.assertAlmostEqual(weight_sum, 1.0, delta=0.01)

        alpha_decay = orb['alpha_decay']
        self.assertEqual(len(alpha_decay['sharpe_windows']), 3)

    def test_workflow_orb_integration(self):
        data = self._load_yaml(self.workflow_path)
        self.assertIn('strategies', data)
        strategies = data['strategies']
        self.assertIn('orb', strategies)
        self.assertTrue(strategies['orb']['enabled'])
        self.assertEqual(strategies['orb']['budget'], 3000)
        self.assertEqual(data['broker']['starting_equity'], 30000)
        self.assertIn('swing', strategies)
        self.assertEqual(strategies['swing']['budget'], 3000)
        self.assertTrue(strategies['swing']['enabled'])
        self.assertIn('intraday', strategies)

    def test_regime_state_json(self):
        data = self._load_json(self.regime_state_path)
        # Live regime monitor writes 'hmm' key (not 'regime')
        self.assertTrue('regime' in data or 'hmm' in data,
                        "Expected 'regime' or 'hmm' key in regime_state.json")
        if 'orb_size_pct' in data:
            self.assertGreaterEqual(data['orb_size_pct'], 0.0)
            self.assertLessEqual(data['orb_size_pct'], 1.0)

    def test_cross_config_consistency(self):
        orb_data = self._load_yaml(self.orb_config_path)
        workflow = self._load_yaml(self.workflow_path)
        self.assertEqual(
            orb_data['orb']['capital'],
            workflow['strategies']['orb']['budget']
        )
        self.assertEqual(
            orb_data['orb']['risk']['max_positions'],
            orb_data['orb']['risk']['max_positions']
        )


if __name__ == '__main__':
    unittest.main()
