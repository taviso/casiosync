# Casio G-Shock Sync

This repository contains scripts for syncing a Casio ABL-100WE watch from Linux.

This is based on [gshock\_api](https://github.com/izivkov/gshock_api/) by @izivkov.

The core addition is figuring out the format of the proprietary lifelog buffer.

# Usage

You can run `casiosync.py` from the commandline:

```
$ ./casiosync.py --help
usage: casiosync.py [-h] [--addr ADDR] [--timeout TIMEOUT] [--peek] [--log] [--quiet]

Sync G-Shock lifelog data.

options:
  -h, --help         show this help message and exit
  --addr ADDR        MAC address of the watch to connect to directly
  --timeout TIMEOUT  Connection timeout in seconds (-1 for infinite)
  --peek             Fetch lifelog without clearing the watch's hourly buffer
  --log              Print the lifelog in a structured log format to stdout
  --quiet            Reduce log output on stderr (only print errors)
```

I use `casiosync.py` from cron, like this:

```
# Sync casio watch steps
29 0,6,12,18 * * * python ~/projects/casio/casiosync.py --addr XX:XX:XX:XX:XX:XX --log --timeout 180 --quiet | logger --tag casio
```

This puts all the data into syslog, which I can query from splunk.

# Data

I collected the lifelog buffer over a few days of usage, these are intended for validation and testing.

The lifelog buffer format is complex, and not entirely understood.

You can examine logs like this:

```
$ ./lifelog.py logs/260801071501.txt
logs/260801071501.txt
  Notes:
    Display reads 1485 steps.
    A 15 minute walk starting 07:00, all 1485 steps occurred during this period.
  Captured:  2026-08-01 07:15:01
  Steps:     1,485
  Distance:  572 m
  Integrity: steps 1,485/1,485 (OK), distance 572/572 (OK), BCD OK

  Hourly activity (five intensity buckets, lowest to highest)
    none committed
    07:00  1,485 steps  intensity=(221, 1164, 100)  [pending]

  Distance components (metres, newest first)
    committed=none
    pending=572

  Previous day detail (2026-07-31)
    Stored total: 10,070 steps, 3,793 m
    Recovered:    10,070/10,070 steps (complete)
    06:30  553 steps  components=(543, 10)
    07:00  734 steps  components=(681, 53)
    07:30  27 steps  components=(27, 0)
    09:00  1,731 steps  components=(1473, 258)
    09:30  2,130 steps  components=(1080, 1050)
    10:00  60 steps  components=(0, 60)
    11:00  108 steps  components=(56, 52)
    11:30  555 steps  components=(44, 511)
    12:00  60 steps  components=(28, 32)
    13:30  421 steps  components=(38, 383)
    14:00  2,042 steps  components=(976, 1066)
    14:30  90 steps  components=(40, 50)
    16:00  36 steps  components=(36, 0)
    16:30  47 steps  components=(47, 0)
    12:00    123 steps  intensity=(41, 82, 0, 0, 0)
    13:00  1,326 steps  intensity=(50, 1236, 40, 0, 0)
    15:00     27 steps  intensity=(27, 0, 0, 0, 0)

  Earlier daily summaries
    2026-07-30: 1,521 steps, 585 m

  Unknown fields
    @244-245 (2 bytes)
      hex: 00 00
      u16: 0
```
