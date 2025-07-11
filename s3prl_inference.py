import torch
import torchaudio
import s3prl
import warnings
import pandas as pd
import argparse
from pathlib import Path
import tempfile
import requests
import os

warnings.filterwarnings("ignore")

def download_if_needed(path):
    """Download file if path is a URL, else return path unchanged."""
    if str(path).startswith("http://") or str(path).startswith("https://"):
        response = requests.get(path)
        response.raise_for_status()
        suffix = os.path.splitext(str(path))[-1]
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
        tmp.write(response.content)
        tmp.close()
        return tmp.name
    return path

class S3PRLModel:
    def __init__(self, ckpt, dict_path='dict.txt'):
        from s3prl.downstream.runner import Runner

        # Support local or remote paths for ckpt and dict
        ckpt = download_if_needed(ckpt)
        dict_path = download_if_needed(dict_path)

        torch.serialization.add_safe_globals([argparse.Namespace])
        model_dict = torch.load(ckpt, map_location='cpu', weights_only=False)
        self.args = model_dict['Args']
        self.config = model_dict['Config']
        
        # Config patch
        self.args.init_ckpt = ckpt
        self.args.device = "cuda" if torch.cuda.is_available() else "cpu"
        self.config['downstream_expert']['text']["vocab_file"] = dict_path
        self.config['runner']['upstream_finetune'] = False
        self.config['runner']['layer_drop'] = False
        self.config['runner']['downstream_pretrained'] = None
        
        runner = Runner(self.args, self.config)
        self.upstream = runner._get_upstream()
        self.featurizer = runner._get_featurizer()
        self.downstream = runner._get_downstream()
        # For temp file cleanup
        self._temp_ckpt = ckpt if ckpt.startswith(tempfile.gettempdir()) else None
        self._temp_dict = dict_path if dict_path.startswith(tempfile.gettempdir()) else None

    def __call__(self, wav_path):
        wav, sr = torchaudio.load(wav_path)
        wav = wav.mean(0).unsqueeze(0)  # Convert to mono
        wav = wav.to(self.args.device)
        
        # Prepare dummy inputs
        dummy_split = "inference"
        dummy_filenames = [Path(wav_path).stem]  # Use filename as ID
        dummy_records = {"loss": [], "hypothesis": [], "groundtruth": [], "filename": []}
        
        with torch.no_grad():
            features = self.upstream.model(wav)
            features = self.featurizer.model(wav, features)
            dummy_labels = [[] for _ in features]  # Empty labels
            self.downstream.model(dummy_split, features, dummy_labels, dummy_filenames, dummy_records)
            predictions = dummy_records["hypothesis"]
        
        return predictions

    def cleanup(self):
        # Clean up downloaded temporary files
        for f in [self._temp_ckpt, self._temp_dict]:
            if f and os.path.isfile(f):
                os.remove(f)

def process_directory(ckpt, dict_path, wav_dir, output_csv):
    model = S3PRLModel(ckpt, dict_path)
    wav_files = list(Path(wav_dir).glob("*.wav"))
    results = []
    
    for wav_file in wav_files:
        output = model(str(wav_file))
        print(output)
        results.append({"filename": wav_file.name, "output": output})
    
    df = pd.DataFrame(results)
    df.to_csv(output_csv, index=False)
    print(f"Results saved to {output_csv}")
    # Clean up temp files if any
    model.cleanup()

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Process WAV files using S3PRL model.")
    parser.add_argument("--ckpt", type=str, required=True, help="Path or URL to model checkpoint.")
    parser.add_argument("--dict_path", type=str, required=True, help="Path or URL to dictionary file.")
    parser.add_argument("--wav_dir", type=str, required=True, help="Directory containing WAV files.")
    parser.add_argument("--output_csv", type=str, required=True, help="Path to output CSV file.")
    
    args = parser.parse_args()
    process_directory(args.ckpt, args.dict_path, args.wav_dir, args.output_csv)
