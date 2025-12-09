import torch

# Check if CUDA is available
print("CUDA available:", torch.cuda.is_available())

# (Optional) If you want more detail:
if torch.cuda.is_available():
    print("  Device count:", torch.cuda.device_count())
    for i in range(torch.cuda.device_count()):
        print(f"  [{i}]", torch.cuda.get_device_name(i))
