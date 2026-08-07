How to generate the Windows V16 executable
==========================================

Option A - One click on a Windows build computer
------------------------------------------------
1. Install 64-bit Python 3.12 once on the BUILD computer.
2. Double-click build_exe.bat.
3. Wait for BUILD SUCCESS.
4. Open dist\OmniBCI_V16\.
5. Send that whole folder (or zip it) to the target computer.
6. The target computer double-clicks OmniBCI_V16.exe. No Python installation is needed there.

Option B - GitHub Actions (builds on a clean Windows runner)
------------------------------------------------------------
1. Commit this folder, including .github/workflows/build-v16-windows-exe.yml.
2. Open the repository Actions page.
3. Run "Build OmniBCI V16 Windows EXE".
4. Download the artifact named OmniBCI_V16_Windows.

The executable uses the V16 cross-PC serial/BLE architecture. It is not a
separate older GUI build.
