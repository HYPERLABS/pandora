#!/bin/bash
#This script will setup the environment for development.
#Use "source ./set_env_development.sh" to source.
#Include in ~/.bashrc for convenience.
script_dir=$( cd -- "$( dirname -- "${BASH_SOURCE[0]}" )" &> /dev/null && pwd )

# Environment variables.
repo_root=$(realpath $script_dir/../../..)
export GREENLAND_ROOT=$repo_root
export GREENLAND_SCRIPTS=$repo_root/scripts
export VIRTUAL_ENV_DISABLE_PROMPT=1

# Custom commands.
pulser-activate-venv() {
    cd $GREENLAND_ROOT/src/pulser/python
    source .venv/bin/activate
}

pulser-run-pynb() {
    cd $GREENLAND_ROOT/src/pulser/python
    source .venv/bin/activate
    jupyter lab --ip='*' --no-browser --port=9999
    deactivate
    cd -
}

mtdr-activate-venv() {
    cd $GREENLAND_ROOT/src/mtdr/python
    source .venv/bin/activate
}

mtdr-run-pynb() {
    cd $GREENLAND_ROOT/src/mtdr/python
    source .venv/bin/activate
    jupyter lab --ip='*' --no-browser --port=9999
    deactivate
    cd -
}

# Allow forward search (i-search).
stty -ixon

# Workaround to enable tab autocomplete with environment variables.
shopt -s direxpand

# A new shell gets the history lines from all previous shells.
PROMPT_COMMAND='history -a'

# If there are multiple matches for completion, Tab should cycle through them and Shift-Tab should cycle backwards.
bind 'TAB:menu-complete'
bind '"\e[Z": menu-complete-backward'

# Display a list of the matching files.
bind "set show-all-if-ambiguous on"

# Perform partial (common) completion on the first Tab press, only start cycling full results on the second Tab press (from bash version 5).
bind "set menu-complete-display-prefix on"

# Cycle through history based on characters already typed on the line.
bind '"\e[A":history-search-backward'
bind '"\e[B":history-search-forward'

# Keep Ctrl-Left and Ctrl-Right working when the above are used.
bind '"\e[1;5C":forward-word'
bind '"\e[1;5D":backward-word'
