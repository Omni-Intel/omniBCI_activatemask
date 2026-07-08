# activeMask PC App

Recommended on this Windows machine:

```powershell
.\run_gui_D_env.cmd
```

This uses `D:\conda_envs\gaobo_bci_active_mask\python.exe` directly and avoids
Conda activation scripts under the Chinese Windows user path.

Fallback named-env workflow, not recommended on this machine because the env may
live under the Chinese Windows user path:

```powershell
conda create -n gaobo_bci_active_mask --override-channels -c conda-forge python=3.11 pip -y
conda run -n gaobo_bci_active_mask python -m pip install -r requirements.txt
conda activate gaobo_bci_active_mask
python active_mask_gui.py
```

Recommended workflow:

1. Connect `COM3` at `921600`.
2. Select active channels and click `Apply Mask`.
3. Press `Query ?` and confirm `BIAS_SENSP` and `CHnSET` values.
4. Press `Record Bin`, then `Start Stream`.
5. Press `Stop Stream`, then `Record Bin` again to close the file and write JSON metadata.

The GUI intentionally does not plot live data. Use the existing bin-to-CSV/MNE
scripts in `D:\高博_采集板优化\采集数据` for offline plotting.
