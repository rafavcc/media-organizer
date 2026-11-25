# Bash tab completion for media-organizer, with a description next to each option (like gh).
#
# Needs: python3-argcomplete and the launcher in ~/.local/bin (see "Tab completion" in the README).
# Use it by adding this line to ~/.bashrc (and removing any `register-python-argcomplete` line):
#   source /home/rafavcc/dev/media-organizer/deploy/media-organizer.bash

_media_organizer_complete() {
    local IFS=$'\n' line name out width=0 i
    local -a names=() descs=()

    # Ask the program for the candidates; _ARGCOMPLETE_DFS makes it append "<TAB>description".
    out=$(_ARGCOMPLETE=1 _ARGCOMPLETE_SHELL=bash _ARGCOMPLETE_IFS=$'\n' _ARGCOMPLETE_DFS=$'\t' \
          _ARGCOMPLETE_SUPPRESS_SPACE=1 _ARGCOMPLETE_COMP_WORDBREAKS="$COMP_WORDBREAKS" \
          COMP_LINE="$COMP_LINE" COMP_POINT="$COMP_POINT" COMP_TYPE="$COMP_TYPE" \
          "$1" 8>&1 9>/dev/null 1>/dev/null 2>/dev/null) || { unset COMPREPLY; return; }
    [[ -n $out ]] || { unset COMPREPLY; return; }  # nothing to offer: fall back to file names

    while read -r line; do
        name=${line%%$'\t'*}
        names+=("$name")
        if [[ $line == *$'\t'* ]]; then descs+=("${line#*$'\t'}"); else descs+=(""); fi
        (( ${#name} > width )) && width=${#name}
    done <<< "$out"

    compopt +o nospace 2>/dev/null   # add the space after a finished word...
    compopt -o nosort 2>/dev/null    # ...keep the program's order (bash 4.4+)...
    if (( ${#names[@]} == 1 )); then
        COMPREPLY=("${names[0]}")
        [[ ${names[0]} =~ [=/:]$ ]] && compopt -o nospace   # ...except after / = :
        return
    fi
    for i in "${!names[@]}"; do   # several candidates: show "name  (description)", one per line
        if [[ -n ${descs[i]} ]]; then
            printf -v "COMPREPLY[i]" '%-*s  (%s)' "$width" "${names[i]}" "${descs[i]}"
        else
            COMPREPLY[i]=${names[i]}
        fi
    done
}

complete -o nospace -o default -o bashdefault -F _media_organizer_complete media-organizer
