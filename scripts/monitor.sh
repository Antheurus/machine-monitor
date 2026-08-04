#!/usr/bin/env bash

# ─── colors ───────────────────────────────────────────────────────────────────
RESET="\033[0m"
BOLD="\033[1m"
DIM="\033[2m"

BG_HEADER="\033[48;5;235m"
BG_SECTION="\033[48;5;232m"

FG_TITLE="\033[38;5;45m"
FG_SECTION="\033[38;5;214m"
FG_LABEL="\033[38;5;244m"
FG_PID="\033[38;5;220m"
FG_PORT="\033[38;5;119m"
FG_NAME="\033[38;5;255m"
FG_CPU_HIGH="\033[38;5;196m"
FG_CPU_MED="\033[38;5;208m"
FG_CPU_LOW="\033[38;5;148m"
FG_MEM_HIGH="\033[38;5;196m"
FG_MEM_MED="\033[38;5;208m"
FG_MEM_LOW="\033[38;5;74m"
FG_KILL="\033[38;5;203m"
FG_GREEN="\033[38;5;46m"
FG_DIVIDER="\033[38;5;238m"

FG_TEMP_OK="\033[38;5;46m"
FG_TEMP_WARN="\033[38;5;208m"
FG_TEMP_HOT="\033[38;5;196m"

COLS=$(tput cols 2>/dev/null || echo 120)

# ─── helpers ──────────────────────────────────────────────────────────────────
divider() {
  printf "${FG_DIVIDER}"
  printf '─%.0s' $(seq 1 "$COLS")
  printf "${RESET}\n"
}

section_header() {
  local label="$1"
  printf "\n${BOLD}${FG_SECTION}  ▌ %-20s${RESET}\n" "$label"
  divider
}

cpu_color() {
  local val="${1%.*}"
  (( val >= 15 )) && echo "$FG_CPU_HIGH" && return
  (( val >= 5  )) && echo "$FG_CPU_MED"  && return
  echo "$FG_CPU_LOW"
}

mem_color() {
  local val="${1%.*}"
  (( val >= 3 )) && echo "$FG_MEM_HIGH" && return
  (( val >= 1 )) && echo "$FG_MEM_MED"  && return
  echo "$FG_MEM_LOW"
}

bar() {
  local pct="${1%.*}" max=20
  local filled=$(( pct * max / 100 ))
  (( filled > max )) && filled=$max
  local empty=$(( max - filled ))
  local color="$2"
  printf "${color}["
  printf '█%.0s' $(seq 1 $filled) 2>/dev/null
  printf '░%.0s' $(seq 1 $empty)  2>/dev/null
  printf "]${RESET}"
}

temp_color() {
  local val="${1%.*}"
  (( val >= 80 )) && echo "$FG_TEMP_HOT"  && return
  (( val >= 60 )) && echo "$FG_TEMP_WARN" && return
  echo "$FG_TEMP_OK"
}

rss_to_human() {
  local kb="$1"
  if (( kb >= 1048576 )); then
    printf "%.1fG" "$(echo "scale=1; $kb/1048576" | bc)"
  elif (( kb >= 1024 )); then
    printf "%.0fM" "$(echo "scale=0; $kb/1024" | bc)"
  else
    printf "%dK" "$kb"
  fi
}

# ─── header ───────────────────────────────────────────────────────────────────
print_header() {
  local mode="${1:-once}"
  local now
  now=$(date "+%Y-%m-%d %H:%M:%S")
  local load
  load=$(sysctl -n vm.loadavg 2>/dev/null | awk '{print $2,$3,$4}')
  local mem_used mem_total
  mem_used=$(vm_stat | awk '/Pages active/{a=$NF} /Pages wired/{b=$NF} END{printf "%.1fG", (a+b)*4096/1073741824}')
  mem_total=$(sysctl -n hw.memsize 2>/dev/null | awk '{printf "%.0fG", $1/1073741824}')

  printf "${BG_HEADER}${BOLD}"
  printf '─%.0s' $(seq 1 "$COLS")
  printf "${RESET}\n"
  printf "${BOLD}${FG_TITLE}  ⚡ MACHINE MONITOR${RESET}"
  printf "  ${FG_LABEL}time:${RESET} ${FG_NAME}${now}${RESET}"
  printf "  ${FG_LABEL}load:${RESET} ${FG_NAME}${load}${RESET}"
  printf "  ${FG_LABEL}mem:${RESET} ${FG_NAME}${mem_used} / ${mem_total}${RESET}"
  if [[ "$mode" == "dispatch" ]]; then
    printf "  ${DIM}[r] refresh  [q] quit  kill: ${FG_KILL}kill -9 <PID>${RESET}\n"
  else
    printf "  ${DIM}kill: ${FG_KILL}kill -9 <PID>${RESET}\n"
  fi
  printf "${FG_DIVIDER}"
  printf '═%.0s' $(seq 1 "$COLS")
  printf "${RESET}\n"
}

# ─── section 1: servers ───────────────────────────────────────────────────────
print_servers() {
  section_header "SERVERS RUNNING"
  printf "  ${BOLD}${FG_LABEL}%-8s  %-6s  %-8s  %-30s  %s${RESET}\n" \
    "PORT" "PID" "PROTOCOL" "PROCESS" "PATH / SERVICE"
  divider

  lsof -i -P -n 2>/dev/null | awk '/LISTEN/' | sort -t: -k2 -n | \
  while read -r line; do
    local cmd pid user fd type dev size node name
    cmd=$(echo "$line" | awk '{print $1}')
    pid=$(echo "$line" | awk '{print $2}')
    name=$(echo "$line" | awk '{print $9}')
    port=$(echo "$name" | grep -oE ':[0-9]+' | tail -1 | tr -d ':')
    proto=$(echo "$name" | grep -oE 'TCP|UDP' || echo "$line" | awk '{print $8}')
    [[ -z "$port" ]] && continue

    full_cmd=$(ps -p "$pid" -o command= 2>/dev/null | head -c 80)
    short_cmd=$(basename "$(echo "$full_cmd" | awk '{print $1}')")

    # label known services
    label=""
    case "$port" in
      5432) label="${FG_GREEN}● PostgreSQL${RESET}" ;;
      6379) label="${FG_GREEN}● Redis${RESET}" ;;
      3009|3010|3011|3012) label="${FG_GREEN}● Nuxt Dev${RESET}" ;;
      3060|3061|3062|3063) label="${FG_GREEN}● Nuxt Dev${RESET}" ;;
      3008|3009|4000|4001|8080|8000) label="${FG_GREEN}● Backend${RESET}" ;;
      8060) label="${FG_GREEN}● scops${RESET}" ;;
      24678) label="${DIM}HMR websocket${RESET}" ;;
      9277) label="${DIM}Warp internal${RESET}" ;;
      42050) label="${DIM}OneDrive sync${RESET}" ;;
      56*) label="${DIM}rapportd${RESET}" ;;
      *) label="${DIM}${short_cmd}${RESET}" ;;
    esac

    printf "  ${FG_PORT}%-8s${RESET}  ${FG_PID}%-6s${RESET}  ${FG_LABEL}%-8s${RESET}  ${FG_NAME}%-30s${RESET}  %b\n" \
      "$port" "$pid" "TCP" "$short_cmd" "$label"
  done
}

# ─── section 2: top cpu ───────────────────────────────────────────────────────
print_top_cpu() {
  section_header "TOP CPU USAGE"
  printf "  ${BOLD}${FG_LABEL}%-8s  %-6s  %-8s  %-6s  %-22s  %s${RESET}\n" \
    "PID" "%CPU" "%MEM" "RSS" "BAR" "COMMAND"
  divider

  ps aux -r 2>/dev/null | awk 'NR>1 && NR<=16' | \
  while read -r user pid cpu mem vsz rss tt stat started time cmd; do
    human=$(rss_to_human "$rss")
    color=$(cpu_color "$cpu")
    b=$(bar "${cpu%.*}" "$color")
    short=$(basename "$(echo "$cmd" | awk '{print $1}')" | cut -c1-40)
    printf "  ${FG_PID}%-8s${RESET}  %b%-6s${RESET}  ${FG_MEM_LOW}%-8s${RESET}  ${FG_LABEL}%-6s${RESET}  %-22b  ${FG_NAME}%s${RESET}\n" \
      "$pid" "$color" "$cpu" "$mem" "$human" "$b" "$short"
  done
}

# ─── section 3: top ram ───────────────────────────────────────────────────────
print_top_mem() {
  section_header "TOP RAM USAGE"
  printf "  ${BOLD}${FG_LABEL}%-8s  %-6s  %-8s  %-8s  %-22s  %s${RESET}\n" \
    "PID" "%MEM" "%CPU" "RSS" "BAR" "COMMAND"
  divider

  ps aux -m 2>/dev/null | awk 'NR>1 && NR<=16' | \
  while read -r user pid cpu mem vsz rss tt stat started time cmd; do
    human=$(rss_to_human "$rss")
    color=$(mem_color "$mem")
    b=$(bar "${mem%.*}" "$color")
    short=$(basename "$(echo "$cmd" | awk '{print $1}')" | cut -c1-40)
    printf "  ${FG_PID}%-8s${RESET}  %b%-6s${RESET}  ${FG_CPU_LOW}%-8s${RESET}  ${FG_LABEL}%-8s${RESET}  %-22b  ${FG_NAME}%s${RESET}\n" \
      "$pid" "$color" "$mem" "$cpu" "$human" "$b" "$short"
  done
}

# ─── footer ───────────────────────────────────────────────────────────────────
print_footer() {
  local mode="${1:-once}"
  printf "\n"
  divider
  printf "  ${FG_LABEL}Kill a process:${RESET}  ${FG_KILL}${BOLD}kill -9 <PID>${RESET}"
  printf "   ${FG_LABEL}Kill by port:${RESET}   ${FG_KILL}${BOLD}lsof -ti:<PORT> | xargs kill -9${RESET}"
  if [[ "$mode" == "dispatch" ]]; then
    printf "   ${FG_LABEL}Refresh:${RESET} ${FG_GREEN}r${RESET}   ${FG_LABEL}Quit:${RESET} ${FG_KILL}q${RESET}\n"
  else
    printf "   ${DIM}persistent mode: -d / --dispatch${RESET}\n"
  fi
  divider
}

# ─── windowserver diagnosis (shown when WS CPU > 15%) ────────────────────────
diagnose_windowserver() {
  local ws_cpu
  ws_cpu=$(ps aux 2>/dev/null | awk '$11 ~ /WindowServer/{print $3; exit}')
  [[ -z "$ws_cpu" ]] && return
  local ws_int="${ws_cpu%.*}"
  (( ws_int < 15 )) && return

  section_header "WINDOWSERVER DIAGNOSIS"
  printf "  ${FG_TEMP_HOT}${BOLD}WindowServer at ${ws_cpu}%%${RESET} — running live validation:\n\n"
  printf "  ${BOLD}${FG_LABEL}%-30s  %-32s  %s${RESET}\n" "FACTOR" "STATUS" "VERDICT"
  divider

  # 1. Refresh rate / ProMotion
  local res_str=""
  res_str=$(system_profiler SPDisplaysDataType -json 2>/dev/null | python3 -c "
import sys, json
try:
    d = json.load(sys.stdin)
    for item in d.get('SPDisplaysDataType', []):
        for nd in item.get('spdisplays_ndrvs', []):
            r = nd.get('_spdisplays_resolution', '')
            if '@' in r: print(r.strip()); break
except: pass
" 2>/dev/null)
  local is_120hz=0
  echo "$res_str" | grep -qE "@[[:space:]]*(11[89]|120)\." && is_120hz=1
  if (( is_120hz )); then
    printf "  ${FG_NAME}%-30s${RESET}  ${FG_TEMP_HOT}%-32s${RESET}  ${FG_TEMP_HOT}← CAUSE: ProMotion 120Hz compositing${RESET}\n" \
      "Refresh rate" "${res_str}"
  else
    printf "  ${FG_NAME}%-30s${RESET}  ${FG_TEMP_OK}%-32s${RESET}  ${FG_TEMP_OK}✓ not ProMotion${RESET}\n" \
      "Refresh rate" "${res_str:-unknown}"
  fi

  # 2. Active video decoding
  local vt_count=0 vt_cpu="0.0"
  vt_count=$(pgrep "VTDecoderXPCService" 2>/dev/null | wc -l | tr -d ' ')
  if (( vt_count > 0 )); then
    vt_cpu=$(ps aux 2>/dev/null | awk '/VTDecoderXPCService/{s+=$3}END{printf "%.1f",s}')
    printf "  ${FG_NAME}%-30s${RESET}  ${FG_TEMP_WARN}%-32s${RESET}  ${FG_TEMP_WARN}← CAUSE: app/browser decoding video${RESET}\n" \
      "Video decode (VTDecoder)" "${vt_count} instance(s) @ ${vt_cpu}%"
  else
    printf "  ${FG_NAME}%-30s${RESET}  ${FG_TEMP_OK}%-32s${RESET}  ${FG_TEMP_OK}✓ no video decoding${RESET}\n" \
      "Video decode (VTDecoder)" "none"
  fi

  # 3. Screen recording — check known apps + SCStream API usage
  local rec_found=""
  for app in "QuickTime Player" "OBS" "Screenflick" "CleanShot X" "CleanShot" "Kap" "Screenium" \
             "Camtasia" "ScreenFlow" "Snagit" "Rottenwood"; do
    ps aux 2>/dev/null | grep -v grep | grep -qi "${app}" && rec_found="${rec_found}${app} "
  done
  local scstream_count=0
  scstream_count=$(lsof -n 2>/dev/null | grep -cE "SCStreamFrameService|screencaptured" 2>/dev/null)
  scstream_count=${scstream_count//[^0-9]/}
  : "${scstream_count:=0}"
  if [[ -n "$rec_found" ]] || (( scstream_count > 0 )); then
    local rec_label="${rec_found:-SCStream active}"
    printf "  ${FG_NAME}%-30s${RESET}  ${FG_TEMP_HOT}%-32s${RESET}  ${FG_TEMP_HOT}← CAUSE: screen is being captured${RESET}\n" \
      "Screen recording" "${rec_label:0:30}"
  else
    printf "  ${FG_NAME}%-30s${RESET}  ${FG_TEMP_OK}%-32s${RESET}  ${FG_TEMP_OK}✓ no recording detected${RESET}\n" \
      "Screen recording" "none"
  fi

  # 4. Transparency/blur compositing
  local transp
  transp=$(defaults read com.apple.universalaccess reduceTransparency 2>/dev/null || echo "0")
  if [[ "$transp" == "1" ]]; then
    printf "  ${FG_NAME}%-30s${RESET}  ${FG_TEMP_OK}%-32s${RESET}  ${FG_TEMP_OK}✓ reduced — less compositing${RESET}\n" \
      "Transparency" "REDUCED"
  else
    printf "  ${FG_NAME}%-30s${RESET}  ${FG_TEMP_WARN}%-32s${RESET}  ${FG_TEMP_WARN}← FACTOR: blur/vibrancy layers active${RESET}\n" \
      "Transparency" "ON"
  fi

  # 5. Display count
  local disp_count=1
  disp_count=$(system_profiler SPDisplaysDataType 2>/dev/null | grep -c "Resolution:" || echo 1)
  if (( disp_count > 1 )); then
    printf "  ${FG_NAME}%-30s${RESET}  ${FG_TEMP_WARN}%-32s${RESET}  ${FG_TEMP_WARN}← FACTOR: more pixels to composite${RESET}\n" \
      "Displays" "${disp_count} active"
  else
    printf "  ${FG_NAME}%-30s${RESET}  ${FG_TEMP_OK}%-32s${RESET}  ${FG_TEMP_OK}✓${RESET}\n" \
      "Displays" "1 (internal only)"
  fi

  printf "\n"
}

# ─── section 4: thermal load (no °C — Apple Silicon has no reliable public die temp) ─
print_temps() {
  section_header "THERMAL LOAD"
  printf "  ${DIM}No °C sensors here — battery/CLI die readings mislead on Apple Silicon. Use a thermal camera for chassis heat.${RESET}\n"
  divider
  printf "  ${BOLD}${FG_LABEL}%-30s  %s${RESET}\n" "METRIC" "VALUE"
  divider

  local pm_cmd="powermetrics"
  [[ $EUID -ne 0 ]] && sudo -n -l /usr/bin/powermetrics >/dev/null 2>&1 && pm_cmd="sudo powermetrics"

  # Thermal pressure + power draw via powermetrics (needs passwordless sudo)
  if [[ "$pm_cmd" == "sudo powermetrics" ]] || [[ $EUID -eq 0 ]]; then
    local pm_out
    pm_out=$($pm_cmd -n 1 -i 500 --samplers cpu_power,gpu_power,thermal 2>/dev/null)

    local pressure cpu_mw gpu_mw
    pressure=$(echo "$pm_out" | awk '/Current pressure level:/{print $NF}')
    cpu_mw=$(echo "$pm_out"   | awk '/^CPU Power:/{print $3}')
    gpu_mw=$(echo "$pm_out"   | awk '/^GPU Power:/{print $3}' | head -1)

    if [[ -n "$pressure" ]]; then
      local pcolor="$FG_TEMP_OK"
      [[ "$pressure" == "Moderate" ]] && pcolor="$FG_TEMP_WARN"
      [[ "$pressure" == "Heavy" || "$pressure" == "Critical" ]] && pcolor="$FG_TEMP_HOT"
      printf "  ${FG_NAME}%-30s${RESET}  %b%s${RESET}\n" "Thermal pressure" "$pcolor" "$pressure"
    fi
    [[ -n "$cpu_mw" ]] && printf "  ${FG_NAME}%-30s${RESET}  ${FG_LABEL}%s mW${RESET}\n" "CPU power draw" "$cpu_mw"
    [[ -n "$gpu_mw" ]] && printf "  ${FG_NAME}%-30s${RESET}  ${FG_LABEL}%s mW${RESET}\n" "GPU power draw" "$gpu_mw"
  else
    printf "  ${DIM}%-30s  echo \"\$(whoami) ALL=(ALL) NOPASSWD: /usr/bin/powermetrics\" | sudo tee /etc/sudoers.d/powermetrics${RESET}\n" \
      "powermetrics (n/a)"
  fi
}

# ─── static machine info (gathered once) ─────────────────────────────────────
gather_machine_info() {
  _MI_MODEL=$(system_profiler SPHardwareDataType 2>/dev/null | awk -F': ' '/Model Name/{print $2}' | xargs)
  _MI_CHIP=$(sysctl -n machdep.cpu.brand_string 2>/dev/null | sed 's/Apple //')
  _MI_TOTAL_CORES=$(sysctl -n hw.physicalcpu 2>/dev/null)
  _MI_PERF_CORES=$(sysctl -n hw.perflevel0.physicalcpu 2>/dev/null)
  _MI_EFF_CORES=$(sysctl -n hw.perflevel1.physicalcpu 2>/dev/null)
  _MI_RAM=$(sysctl -n hw.memsize 2>/dev/null | awk '{printf "%dGB", $1/1073741824}')
}

print_device_bar() {
  local cores_str
  if [[ -n "$_MI_PERF_CORES" && -n "$_MI_EFF_CORES" ]]; then
    cores_str="${_MI_TOTAL_CORES} (${_MI_PERF_CORES}P+${_MI_EFF_CORES}E)"
  else
    cores_str="${_MI_TOTAL_CORES}"
  fi

  local vm_str
  if pgrep -x "QEMU" >/dev/null 2>&1 || pgrep -x "utmd" >/dev/null 2>&1 || pgrep -x "UTM" >/dev/null 2>&1; then
    vm_str="${FG_CPU_MED}● running${RESET}"
  else
    vm_str="${DIM}none${RESET}"
  fi

  printf "  ${FG_LABEL}device:${RESET} ${FG_NAME}${_MI_MODEL}${RESET}"
  printf "   ${FG_LABEL}chip:${RESET} ${FG_NAME}${_MI_CHIP}${RESET}"
  printf "   ${FG_LABEL}cores:${RESET} ${FG_NAME}${cores_str}${RESET}"
  printf "   ${FG_LABEL}total ram:${RESET} ${FG_NAME}${_MI_RAM}${RESET}"
  printf "   ${FG_LABEL}vm:${RESET} %b\n" "$vm_str"
  divider
}

# ─── main loop ────────────────────────────────────────────────────────────────
REFRESH=3
gather_machine_info

if [[ "$1" == "-d" || "$1" == "--dispatch" ]]; then
  while true; do
    clear
    print_header dispatch
    print_device_bar
    print_temps
    print_servers
    print_top_cpu
    diagnose_windowserver
    print_top_mem
    print_footer dispatch

    read -r -s -n1 -t "$REFRESH" key 2>/dev/null
    case "$key" in
      q|Q) clear; exit 0 ;;
      r|R) continue ;;
    esac
  done
else
  clear
  print_header
  print_device_bar
  print_temps
  print_servers
  print_top_cpu
  diagnose_windowserver
  print_top_mem
  print_footer once
fi
