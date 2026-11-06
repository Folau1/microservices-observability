#!/bin/sh
set -eu
chain=CODEX_K3S_API
setup4() {
  if ! iptables -n -L "$chain" >/dev/null 2>&1; then
    iptables -N "$chain"
    iptables -A "$chain" -s 127.0.0.0/8 -j RETURN
    iptables -A "$chain" -s 192.168.1.0/24 -j RETURN
    iptables -A "$chain" -s 10.42.0.0/16 -j RETURN
    iptables -A "$chain" -j DROP
  fi
  iptables -C "$chain" -s 127.0.0.0/8 -j RETURN
  iptables -C "$chain" -s 192.168.1.0/24 -j RETURN
  iptables -C "$chain" -s 10.42.0.0/16 -j RETURN
  iptables -C "$chain" -j DROP
  for port in 6443 10250; do
    iptables -C INPUT -p tcp --dport "$port" -j "$chain" 2>/dev/null || iptables -I INPUT 1 -p tcp --dport "$port" -j "$chain"
  done
  iptables -C INPUT -p udp --dport 8472 -j "$chain" 2>/dev/null || iptables -I INPUT 1 -p udp --dport 8472 -j "$chain"
}
setup6() {
  if ! ip6tables -n -L "$chain" >/dev/null 2>&1; then
    ip6tables -N "$chain"
    ip6tables -A "$chain" -s ::1/128 -j RETURN
    ip6tables -A "$chain" -j DROP
  fi
  ip6tables -C "$chain" -s ::1/128 -j RETURN
  ip6tables -C "$chain" -j DROP
  for port in 6443 10250; do
    ip6tables -C INPUT -p tcp --dport "$port" -j "$chain" 2>/dev/null || ip6tables -I INPUT 1 -p tcp --dport "$port" -j "$chain"
  done
  ip6tables -C INPUT -p udp --dport 8472 -j "$chain" 2>/dev/null || ip6tables -I INPUT 1 -p udp --dport 8472 -j "$chain"
}
setup4
setup6
