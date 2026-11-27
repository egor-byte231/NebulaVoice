"""Optional OpenVoice V2 tone-color conversion for a private reference voice.

Requires OpenVoice (myshell-ai/OpenVoice), its V2 converter checkpoints, and
the reference recording on the conversion host. No audio is uploaded by this script.
"""
import argparse
from pathlib import Path

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True)
    parser.add_argument("--reference", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--checkpoint-dir", required=True)
    args = parser.parse_args()
    for path in (args.input, args.reference):
        if not Path(path).is_file():
            parser.error("Missing input file: " + path)
    ckpt = Path(args.checkpoint_dir)
    if not (ckpt / "config.json").is_file() or not (ckpt / "checkpoint.pth").is_file():
        parser.error("OpenVoice V2 converter checkpoints are missing")
    import torch
    from openvoice import se_extractor
    from openvoice.api import ToneColorConverter
    device = "cuda:0" if torch.cuda.is_available() else "cpu"
    converter = ToneColorConverter(str(ckpt / "config.json"), device=device)
    converter.load_ckpt(str(ckpt / "checkpoint.pth"))
    source_se, _ = se_extractor.get_se(args.input, converter, vad=False)
    target_se, _ = se_extractor.get_se(args.reference, converter, vad=False)
    converter.convert(
        audio_src_path=args.input, src_se=source_se, tgt_se=target_se,
        output_path=args.output, message="@NebulaVoice",
    )
    if not Path(args.output).is_file() or Path(args.output).stat().st_size == 0:
        raise RuntimeError("Voice conversion produced no output")

if __name__ == "__main__":
    main()
