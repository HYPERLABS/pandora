#!/bin/bash
#This script will compile the pulser examples for Linux.
#Operating systems supported:
# * Ubuntu/Debian/Mint
set -e
while getopts h flag
do
    case "${flag}" in
        h) echo "Usage: ${0##*/}" && exit;;
    esac
done

script_dir=$( cd -- "$( dirname -- "${BASH_SOURCE[0]}" )" &> /dev/null && pwd )
repo_root=$(realpath $script_dir/../../..)
proto_dir=$repo_root/src/pulser/proto

# Get the system name.
uname_out="$(uname -s)"
case "${uname_out}" in
    Linux*)     machine=Linux;;
    Darwin*)    machine=Mac;;
    CYGWIN*)    machine=Windows;;
    MINGW*)     machine=Windows;;
    *)          machine="UNKNOWN:${uname_out}"
esac

echo Building pulser python...
if [[ $machine != "Linux" ]]; then
    echo "This script is only supported in Linux"
    exit 1
fi
if ! command -v python3 >/dev/null 2>&1; then
    echo Please install Python
    exit 1
elif python3 -c "import sys; exit(sys.version_info >= (3, 12))"; then
    echo "Python version installed is $(python3 -V | awk -F' ' '{print $2}'). Version 3.12 or newer is required!"
    exit 1
fi
python_dst_dir=$repo_root/src/pulser/python
cd $python_dst_dir
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install --upgrade pip
python3 -m pip install -r requirements_linux.txt
mkdir -p $python_dst_dir/generated
shopt -s nullglob
for proto_file in "$proto_dir"/*.proto; do
    echo Compiling $proto_file
    python3 -m grpc_tools.protoc -Igenerated=$proto_dir -I"$proto_dir" --python_out="$python_dst_dir" --pyi_out="$python_dst_dir" --grpc_python_out="$python_dst_dir" "$proto_file"
done
echo Completed building
echo "** To interact with the python getting started notebook run: source $repo_root/scripts/linux/setup/set_env.sh ; pulser-run-grpc-example-pynb **"
cd $script_dir
