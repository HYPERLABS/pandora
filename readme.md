# GREENLAND
The repository is structured as follows:
```
├── src
│   ├── pulser
│   │   ├── grpc
│   │   ├── ...
│   ├── mtdr
│   │   ├── grpc
│   │   ├── ...
├── ext
│   ├── ...
├── scripts
```
* `src` - code examples organized by instruments and programming scheme
* `ext` - externals
* `scripts` - setup and build scripts

## Getting Started
The examples are supported on most Linux distributions, Windows and OSx (not tested).

### Python & [grpc](https://grpc.io/)
The following steps compile the examples and demonstrate control using a jupyer notebook.\
Note that python 3.12 or above is required.

#### Linux (Ubuntu/Debian)
1. Make sure you have python 3.12 or newer
2. Install the python virtual environment package using: `sudo apt install python3-venv`
3. Install the python interface to the Tcl/Tk GUI toolkit using: `sudo apt install python3-tk`
4. Run the compilation script corresponding to your instrument from `scripts/linux/build` to create a virtual environment
5. Source the script `set_env.sh` from `scripts/linux/setup`. The script will introduce new command aliases that will run the examples.

#### Windows
1. Make sure you have python 3.12 or newer
2. Make sure that you python installation includes the Tcl/Tk GUI toolkit
3. Run the compilation script corresponding to your instrument from `scripts/windows/cmd/build` or `scripts/windows/ps/build` to create virtual environment
4. Run one of the example scripts in `scripts/windows/cmd/run` or `scripts/windows/ps/run`
