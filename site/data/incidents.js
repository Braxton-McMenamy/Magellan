// written by demo/run.py -- do not edit by hand
window.MAGELLAN_INCIDENTS = [
  {
    "name": "azure-leap-day-2012",
    "title": "Windows Azure, February 29 2012: the leap-day outage",
    "what_happened": "The guest agent in every new VM created a transfer certificate valid until \"the same date next year\", computed by adding one to the year. On February 29 2012 that date, February 29 2013, did not exist; certificate creation failed, VMs failed to start, hosts were marked faulty, and the failures cascaded across clusters for most of a day.",
    "sources": [
      "Microsoft, \"Summary of Windows Azure Service Disruption on Feb 29th, 2012\" (March 2012)"
    ],
    "language": "Python port of the guest agent's date arithmetic.",
    "steps": [
      {
        "dir": "2-same-date-next-year",
        "what": "Valid until the same calendar date next year.",
        "verdict": "ok",
        "status": "waiting",
        "rules": {
          "leap-day-date": "waiting"
        },
        "unwanted": [],
        "known_miss": "",
        "findings": []
      }
    ]
  },
  {
    "name": "cloudflare-waf-2019",
    "title": "Cloudflare, July 2 2019: a WAF rule pins every CPU, 27-minute global outage",
    "what_happened": "A new managed WAF rule shipped with a regular expression whose `.*(?:.*=.*)` part backtracks super-linearly. Rules deploy globally at once, so every edge server's CPU went to 100% matching HTTP requests against it.",
    "sources": [
      "Cloudflare blog, \"Details of the Cloudflare outage on July 2, 2019\" (July 12 2019), which quotes the regex"
    ],
    "language": "Python port (Cloudflare's WAF ran PCRE from Lua); the regex is verbatim.",
    "steps": [
      {
        "dir": "2-new-xss-rule",
        "what": "The new XSS rule, with the regex from Cloudflare's post-mortem.",
        "verdict": "ok",
        "status": "waiting",
        "rules": {
          "regex-catastrophic-backtracking": "waiting"
        },
        "unwanted": [],
        "known_miss": "",
        "findings": []
      }
    ]
  },
  {
    "name": "knight-capital-2012",
    "title": "Knight Capital, August 1 2012: $460 million in 45 minutes",
    "what_happened": "SMARS, Knight's order router, still contained 'Power Peg', unused since 2003. Power Peg sent child orders until a cumulative-fill counter said the parent order was filled; a 2005 change moved that counting earlier in the order flow, so Power Peg could no longer stop. In 2012 new Retail Liquidity Program (RLP) code replaced it and reused its flag. A technician did not copy the new code to one of eight servers; orders carrying the reused flag woke Power Peg up there and it sent millions of orders.",
    "sources": [
      "SEC Release No. 34-70694, In the Matter of Knight Capital Americas LLC (2013)"
    ],
    "language": "Python port (SMARS's source and language are not public).",
    "steps": [
      {
        "dir": "2-2005-fill-tracking-moved",
        "what": "Fill tracking moves out of Power Peg's loop to the start of routing.",
        "verdict": "ok",
        "status": "waiting",
        "rules": {
          "loop-without-progress": "waiting"
        },
        "unwanted": [],
        "known_miss": "",
        "findings": []
      },
      {
        "dir": "3-2012-rlp-reuses-the-flag",
        "what": "RLP replaces Power Peg and takes over its flag value 0x08.",
        "verdict": "ok",
        "status": "waiting",
        "rules": {
          "reused-value": "waiting"
        },
        "unwanted": [],
        "known_miss": "",
        "findings": []
      }
    ]
  },
  {
    "name": "sensor-signature-break",
    "title": "The sensor agent: a signature change breaks a file nobody touched",
    "what_happened": "A three-file change adds a required `layout` argument to parse_record and drops the legacy collection mode. Every edited file is consistent. But collector.py, which nobody touched, still calls parse_record(row) with one argument -- inside `except Exception: pass`, so the TypeError is swallowed and the sweep silently returns nothing.",
    "sources": [
      "Magellan's worked example (examples/sensor and examples/after)"
    ],
    "language": "Python",
    "steps": [
      {
        "dir": "2-layout-argument",
        "what": "parse_record gains a required layout argument; collector.py is not updated.",
        "verdict": "ok",
        "status": "waiting",
        "rules": {
          "signature-break": "waiting"
        },
        "unwanted": [],
        "known_miss": "",
        "findings": []
      }
    ]
  },
  {
    "name": "therac-25-1986",
    "title": "Therac-25, 1985-1987: radiation overdoses from a race condition",
    "what_happened": "The operator could edit the prescription (beam mode and energy) while the treatment task was still setting up the magnets. The two tasks shared those values without synchronisation, so a quick edit was not seen by the setup task: the machine fired a high-current electron beam configured for X-ray mode. The Therac-20 had hardware interlocks that caught this; the Therac-25 relied on software alone. Six known accidents, at least three fatal.",
    "sources": [
      "Nancy Leveson and Clark Turner, \"An Investigation of the Therac-25 Accidents\", IEEE Computer (1993)"
    ],
    "language": "Python port (the original was PDP-11 assembly).",
    "steps": [
      {
        "dir": "2-software-only",
        "what": "The serialisation is gone: an edit can land while setup reads the old values.",
        "verdict": "ok",
        "status": "waiting",
        "rules": {
          "unsynchronized-shared-state": "waiting"
        },
        "unwanted": [],
        "known_miss": "",
        "findings": []
      }
    ]
  },
  {
    "name": "zune-2008",
    "title": "Microsoft Zune 30, December 31 2008: every device freezes on boot",
    "what_happened": "The real-time-clock driver turned days since 1980 into a year with a loop that subtracts 365 or 366 days per year. On the last day of a leap year (day 366 of 2008) neither branch changed anything: the loop spun forever and the players hung at boot until the battery ran down.",
    "sources": [
      "Freescale MC13783 RTC driver, ConvertDays() in rtc.c, as published after the incident; Microsoft Zune support statement (January 2009)"
    ],
    "language": "Python port of the C driver.",
    "steps": [
      {
        "dir": "2-days-to-year",
        "what": "Convert the day count to a calendar year (the driver's ConvertDays).",
        "verdict": "ok",
        "status": "waiting",
        "rules": {
          "loop-without-progress": "waiting"
        },
        "unwanted": [],
        "known_miss": "",
        "findings": []
      },
      {
        "dir": "3-fix-break-on-day-366",
        "what": "The fix: leave the loop on day 366 of a leap year.",
        "verdict": "ok",
        "status": "quiet",
        "rules": {},
        "unwanted": [],
        "known_miss": "",
        "findings": []
      }
    ]
  }
];