#apache licensed
#!/usr/bin/env python3
# NatureMS Core Pipeline - Hardware Optimized Structure Elucidation

import argparse
import os
import json
import torch
import subprocess
import numpy as np
import cv2
import pandas as pd
import pymzml
from pathlib import Path
from typing import Dict, Any, List
from rdkit import Chem
from rdkit.Chem import AllChem, Descriptors
from sklearn.cluster import DBSCAN
from scipy.signal import savgol_filter
from sklearn.ensemble import RandomForestClassifier

# Local imports
from molformer import MolFormer
from chiral import StereoNet, smiles_to_graph

class NatureMS:
    """Main pipeline class handling end-to-end processing"""

    def __init__(self, config: Dict[str, Any]):
        self.config = config
        self.device = self._configure_hardware()

        # Initialize core models
        self.molformer = MolFormer().to(self.device)
        self.stereonet = StereoNet().to(self.device)
        self.rf_classifier = RandomForestClassifier(n_estimators=100, max_depth=10)  # Train separately
        self.rf_peak_scorer = RandomForestClassifier(n_estimators=50, max_depth=5)  # For peak scoring

        # Adduct mass mapping (Da)
        self.adduct_masses = {
            'M': 0.0, 'M+H': 1.0078, 'M-H': -1.0078, 'M+Na': 22.9898, 'M+NH4': 18.0338
        }

        # Neutral loss mapping (Da)
        self.neutral_losses = {
            'H2O': 18.0106, 'CO2': 44.0095, 'NH3': 17.0265, 'CH2O': 30.0106
        }

        # Biogenic templates
        self.templates = {
            'D-sugars': {'C2': 'R'},
            'L-amino acids': {'C_alpha': 'S'},
            'taxane': {'C6': 'R', 'C9': 'S'}
        }

        # Ensemble weights
        self.ensemble_weights = {'rules': 0.3, 'ml': 0.4, 'sirius': 0.3}

        # SMILES vocabulary for MolFormer decoding
        self.smiles_vocab = list("CcNnoOsS(=)[]1234567890@#H+-")  # Simplified SMILES characters

    def _configure_hardware(self) -> torch.device:
        """Set hardware context based on user choice"""
        if self.config['runtime'] == 'colab':
            torch.set_float32_matmul_precision('high')
            return torch.device('cuda' if torch.cuda.is_available() else 'cpu')
        return torch.device('cpu')

    def _optimize_hardware(self, data_size: int) -> None:
        """Optimize hardware usage based on data size"""
        if self.config['runtime'] == 'colab' and torch.cuda.is_available():
            torch.cuda.empty_cache()  # Clear GPU memory
            if data_size > 1000:  # Threshold for large datasets
                torch.cuda.set_per_process_memory_fraction(0.8)  # Limit to 80% GPU memory

    def _load_input_data(self, input_file: Path) -> Dict[str, Any]:
        """Preprocess input data with advanced peak detection"""
        ext = input_file.suffix.lower()

        if ext in ['.mzml', '.mzxml']:
            run = pymzml.run.Reader(str(input_file))
            peaks = [(spec.mz, spec.i) for spec in run if spec.ms_level == 1]
            peaks = np.array(peaks)
        elif ext == '.csv':
            df = pd.read_csv(input_file, usecols=['m/z', 'intensity'])
            peaks = df[['m/z', 'intensity']].to_numpy()
        elif ext in ['.png', '.jpg']:
            img = cv2.imread(str(input_file))
            gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
            entropy = -np.sum([p * np.log2(p + 1e-10) for p in np.histogram(gray, bins=256, density=True)[0]])
            noise_factor = 1.0 if entropy < 5.0 else 2.0
            blurred = cv2.GaussianBlur(gray, (5, 5), sigmaX=noise_factor)  # 5x5 per proposal
            _, thresh = cv2.threshold(blurred, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
            kernel = np.ones((3, 3), np.uint8)
            cleaned = cv2.morphologyEx(thresh, cv2.MORPH_OPEN, kernel)
            profile = np.mean(cleaned, axis=0)
            profile = savgol_filter(profile, window_length=11, polyorder=2)  # Savitzky-Golay
            peak_threshold = np.percentile(profile, 90)
            peaks_idx = (profile > peak_threshold) & (np.r_[profile[1:], 0] < profile) & (np.r_[0, profile[:-1]] < profile)
            mz_range = (50, 1000)
            mz_values = np.linspace(mz_range[0], mz_range[1], len(profile))
            peaks = np.array([[mz_values[i], profile[i]] for i in range(len(profile)) if peaks_idx[i]])
        else:
            raise ValueError(f"Unsupported file format: {ext}")

        if len(peaks) == 0:
            raise ValueError("No peaks detected in input data")

        # Noise filtering
        base_intensity = peaks[:, 1].max()
        peaks = peaks[peaks[:, 1] >= 0.01 * base_intensity]

        # Deisotoping with DBSCAN
        clustering = DBSCAN(eps=0.02, min_samples=2).fit(peaks[:, 0].reshape(-1, 1))
        labels = clustering.labels_
        monoisotopic_peaks = []
        for label in set(labels) - {-1}:  # Exclude noise
            cluster = peaks[labels == label]
            monoisotopic_peaks.append(cluster[cluster[:, 0].argmin()])  # Lowest m/z
        peaks = np.array(monoisotopic_peaks) if monoisotopic_peaks else peaks

        # Memory-efficient batching
        peaks_df = pd.DataFrame(peaks, columns=['m/z', 'intensity'])
        self._optimize_hardware(len(peaks_df))

        # Random Forest peak scoring
        peak_features = np.column_stack([
            peaks[:, 0],  # m/z
            peaks[:, 1],  # intensity
            [abs(peaks[i, 0] - peaks[i-1, 0]) if i > 0 else 0 for i in range(len(peaks))]  # m/z diff
        ])
        peak_scores = self.rf_peak_scorer.predict_proba(peak_features)[:, 1] if hasattr(self.rf_peak_scorer, 'predict_proba') else np.ones(len(peaks))
        peaks_df['score'] = peak_scores

        # Top fragment selection
        top_peaks = peaks_df.sort_values('score', ascending=False).head(10).to_numpy()  # Top 10 peaks
        peaks = top_peaks[:, :2]  # Keep only m/z and intensity

        peaks[:, 1] /= peaks[:, 1].max()
        precursor_mz = peaks[peaks[:, 1].argmax(), 0]
        neutral_mass = precursor_mz - self.adduct_masses[self.config['ion_mode']]

        frag_tree = {'precursor': precursor_mz, 'fragments': []}
        for mz, intensity in peaks:
            if mz < precursor_mz - 5:
                for loss, mass in self.neutral_losses.items():
                    if abs(precursor_mz - mz - mass) < 0.02:
                        frag_tree['fragments'].append({'m/z': mz, 'intensity': intensity, 'loss': loss})
                        break
                else:
                    frag_tree['fragments'].append({'m/z': mz, 'intensity': intensity})

        return {'mass': neutral_mass, 'tree': frag_tree}

    def _predict_class(self, processed_data: Dict[str, Any]) -> str:
        """Compound class prediction with Random Forest"""
        features = [processed_data['mass']]
        for loss in self.neutral_losses:
            features.append(1 if any(f.get('loss') == loss for f in processed_data['tree']['fragments']) else 0)
        features.append(0)  # Placeholder for 13C/12C ratio
        pred = self.rf_classifier.predict([features])[0]  # Train with COCONUT/PubChem
        classes = ['terpenes', 'alkaloids', 'carbohydrates', 'polyketides', 'lipids', 'shikimates', 'peptides', 'organometallics']
        return classes[pred] if pred < len(classes) else 'large_organic'

    def _run_sirius(self, processed_data: Dict[str, Any]) -> str:
        """Execute SIRIUS with metal support"""
        temp_ms = Path(self.config['output_dir']) / 'temp.ms'
        with open(temp_ms, 'w') as f:
            f.write(f"{processed_data['tree']['precursor']}\n")
            for frag in processed_data['tree']['fragments']:
                f.write(f"{frag['m/z']}\n")
        cmd = [
            'sirius', '-i', str(temp_ms), '-o', str(self.config['output_dir'] / 'sirius_smiles.txt'),
            '--elements', 'Fe,Mg,Co', '--profile', 'orbitrap', '--ppm-max', '10'
        ]
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            raise RuntimeError(f"SIRIUS execution failed: {result.stderr}")
        with open(self.config['output_dir'] / 'sirius_smiles.txt', 'r') as f:
            return f.read().strip()

    def _mz_to_tokens(self, frag_mzs: List[float]) -> torch.Tensor:
        """Convert m/z values to token indices for MolFormer"""
        tokens = [min(int(mz / 10), 299) for mz in frag_mzs]  # Map m/z to 0-299 range
        return torch.tensor(tokens, dtype=torch.long, device=self.device).unsqueeze(0)

    def _decode_smiles(self, logits: torch.Tensor) -> str:
        """Decode MolFormer logits to SMILES string"""
        token_ids = logits.argmax(-1)[0].cpu().numpy()  # Get most likely tokens
        smiles = ''
        for token_id in token_ids:
            if token_id < len(self.smiles_vocab):
                smiles += self.smiles_vocab[token_id]
            else:
                break  # Stop at unknown token
        return smiles

    def _assemble_structures(self, processed_data: Dict[str, Any]) -> List[str]:
        """Hybrid structure assembly with RL-inspired iterative refinement"""
        compound_class = self._predict_class(processed_data)
        neutral_mass = processed_data['mass']

        # RL-inspired iterative refinement for rule-based assembly
        best_smiles = None
        best_reward = -float('inf')
        max_iterations = 5  # Number of refinement iterations
        base_units = int(neutral_mass / 68) or 1  # Initial guess for terpene units (C5H8 = 68 Da)

        for iteration in range(max_iterations):
            # Rule-based assembly (adjust units based on iteration)
            if compound_class == 'terpenes':
                units = base_units + (iteration - max_iterations // 2)
                if units < 1:
                    units = 1
                rules_smiles = 'CC(C)=C' * units
            elif compound_class == 'peptides':
                units = max(1, base_units // 100 + (iteration - max_iterations // 2))
                rules_smiles = 'CC(N)C(=O)' * units
            else:
                rules_smiles = None

            # Evaluate the SMILES (calculate reward)
            if rules_smiles:
                mol = Chem.MolFromSmiles(rules_smiles)
                if mol:
                    calc_mass = Descriptors.ExactMolWt(mol)
                    mass_error = abs(calc_mass - neutral_mass) / neutral_mass
                    reward = 1 - mass_error  # Higher reward for lower mass error
                    if reward > best_reward:
                        best_reward = reward
                        best_smiles = rules_smiles
                    if mass_error < 0.001:  # 0.1% error threshold
                        break
            else:
                reward = -float('inf')

        # MolFormer
        frag_mzs = [processed_data['tree']['precursor']] + [f['m/z'] for f in processed_data['tree']['fragments']]
        tokens = self._mz_to_tokens(frag_mzs)
        with torch.no_grad():
            logits = self.molformer(tokens)
            ml_smiles = self._decode_smiles(logits)

        # SIRIUS
        sirius_smiles = self._run_sirius(processed_data)

        # Use the best SMILES from RL loop if available
        rules_smiles = best_smiles if best_smiles else None
        candidates = [s for s in [rules_smiles, ml_smiles, sirius_smiles] if s]
        if not candidates:
            raise RuntimeError("No valid structures assembled")

        # Ensemble consensus with weighted voting
        if len(candidates) > 1:
            scores = {
                'rules': 0.5 if rules_smiles else 0.0,
                'ml': 0.7 if ml_smiles else 0.0,
                'sirius': 0.9 if sirius_smiles else 0.0
            }
            weighted_scores = {k: scores[k] * self.ensemble_weights[k] for k in scores}
            best_method = max(weighted_scores, key=weighted_scores.get)
            if best_method == 'rules' and rules_smiles:
                candidates = [rules_smiles]
            elif best_method == 'ml' and ml_smiles:
                candidates = [ml_smiles]
            elif best_method == 'sirius' and sirius_smiles:
                candidates = [sirius_smiles]

        return candidates

    def _resolve_stereochemistry(self, candidates: List[str]) -> List[Dict[str, Any]]:
        """Resolve stereochemistry with templates and StereoNet"""
        stereo_candidates = []
        for smiles in candidates:
            mol = Chem.MolFromSmiles(smiles)
            if not mol:
                continue

            # Biogenic templates
            stereo_smiles = smiles
            for template, configs in self.templates.items():
                if template in ['D-sugars', 'L-amino acids', 'taxane']:
                    for pos, config in configs.items():
                        stereo_smiles = stereo_smiles.replace('C', f'[C@H]' if config == 'R' else '[C@@H]', 1)

            # StereoNet
            data = smiles_to_graph(stereo_smiles, self.device)
            if not data:
                continue
            with torch.no_grad():
                stereo_pred = self.stereonet(data)
                chiral_centers = [a.GetIdx() for a in mol.GetAtoms() if a.GetChiralTag() != Chem.ChiralType.CHI_UNSPECIFIED]
                for idx, center in enumerate(chiral_centers[:10]):
                    if stereo_pred[center, 1] > 0.5:
                        stereo_smiles = stereo_smiles.replace('C', '[C@@H]', 1)
                    else:
                        stereo_smiles = stereo_smiles.replace('C', '[C@H]', 1)

            # Energy validation (MMFF94 then GFN2-xTB)
            mol = Chem.MolFromSmiles(stereo_smiles)
            if mol:
                mol = Chem.AddHs(mol)
                AllChem.EmbedMolecule(mol, maxAttempts=10)
                ff = AllChem.MMFFGetMoleculeForceField(mol)
                mmff_energy = ff.CalcEnergy() if ff else float('inf')
                if mmff_energy < 10:  # Pruning threshold
                    # Mock GFN2-xTB (replace with xtb-python)
                    gfn_energy = mmff_energy - 2  # Placeholder adjustment
                    stereo_candidates.append({'smiles': stereo_smiles, 'energy': gfn_energy})
        if not stereo_candidates:
            raise RuntimeError("No valid stereochemistry resolved")
        return stereo_candidates

    def _validate_candidates(self, stereo_candidates: List[Dict[str, Any]], processed_data: Dict[str, Any]) -> Dict[str, Any]:
        """Validate candidates based on energy"""
        # Simplified validation: select candidate with lowest energy
        return min(stereo_candidates, key=lambda x: x['energy'])

    def _save_results(self, final_structure: Dict[str, Any], processed_data: Dict[str, Any], stereo_candidates: List[Dict[str, Any]]):
        """Generate SMILES + JSON output"""
        mol = Chem.MolFromSmiles(final_structure['smiles'])
        formula = Chem.rdMolDescriptors.CalcMolFormula(mol) if mol else 'Unknown'
        centers = sum(1 for a in mol.GetAtoms() if a.GetChiralTag() != Chem.ChiralType.CHI_UNSPECIFIED) if mol else 0
        result = {
            'smiles': final_structure['smiles'],
            'formula': formula,
            'neutral_mass': processed_data['mass'],
            'stereochemistry': {
                'centers': centers,
                'method': 'StereoNet + Templates'
            },
            'isomers': [{'energy_kcal/mol': cand['energy'], 'energy_rank': i + 1}
                       for i, cand in enumerate(sorted(stereo_candidates, key=lambda x: x['energy']))][:2],
            'fragments': [{'m/z': f['m/z'],
                          'intensity': f['intensity'],
                          'annotation': f.get('loss', 'unknown loss')}
                          for f in processed_data['tree']['fragments']]
        }
        with open(Path(self.config['output_dir']) / 'output.txt', 'w') as f:
            f.write(f"{result['smiles']}\n{json.dumps(result, indent=2)}")

    def process(self, input_file: Path):
        """Execute full processing pipeline"""
        raw_data = self._load_input_data(input_file)
        candidates = self._assemble_structures(raw_data)
        stereo_candidates = self._resolve_stereochemistry(candidates)
        final_structure = self._validate_candidates(stereo_candidates, raw_data)
        self._save_results(final_structure, raw_data, stereo_candidates)

if __name__ == '__main__':
    parser = argparse.ArgumentParser(
        description='NatureMS: De Novo Structure Elucidation Pipeline',
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    parser.add_argument('input', type=str, help='Path to input file (mzML/CSV/PNG)')
    parser.add_argument('--ion-mode', type=str, required=True,
                        choices=['M', 'M+H', 'M-H', 'M+Na', 'M+NH4'], help='Ionization mode')
    parser.add_argument('--runtime', type=str, required=True,
                        choices=['local', 'colab'], help='Execution environment')
    parser.add_argument('--output-dir', type=str, default='./results', help='Output directory')
    args = parser.parse_args()
    Path(args.output_dir).mkdir(parents=True, exist_ok=True)
    pipeline = NatureMS(vars(args))
    pipeline.process(Path(args.input))

#end
