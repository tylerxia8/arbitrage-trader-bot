# Where the partitions are, 2026-08-14

A structural sweep of every open event the venue lists. Run against the **demo**
host, which publishes the same market definitions as production while
production is refusing this address. Structure is a property of the definition,
so this answer holds; pricing is not, and no book was fetched.

## The numbers

| | |
| --- | --- |
| open events examined | 11,297 |
| distinct series | 3,696 |
| **verified partitions** | **88** |
| across distinct series | 47 |
| rejected: not a partition | 11,133 |
| rejected: buckets do not tile | 76 |

**Under one percent of this venue is shaped like a basket.** The overwhelming
majority of events are binary or enumerated — mutually exclusive without being
collectively exhaustive, which is the difference between a basket that pays a
guaranteed dollar and a set of bets that might all lose.

The 76 that classify as partitions and then fail integer coverage are worth
noting separately: those are sets that *look* exhaustive and leave an outcome
unresolved. Summing one produces a discount that is really a missing leg. They
are the reason the coverage check exists.

## Of the 88, almost everything is weather

40 of the 47 series are daily high or low temperature (`KXHIGH*`, `KXLOW*`),
six legs each. That is the family already measured, and measured negative twice:
crossing the spread has no room, and quoting has room that cannot be reached
because the basket never completes.

Seven series are not weather:

| series | legs | example |
| --- | --- | --- |
| `KXTRUTHSOCIAL` | 10 | `KXTRUTHSOCIAL-26AUG22` |
| `KXEOTRUMPTERM` | 12 | `KXEOTRUMPTERM-29JAN20` |
| `KXSWENCOUNTERS` | 9 | `KXSWENCOUNTERS-25OCT` |
| `KXDSENATESEATSH` | 7 | `KXDSENATESEATSH-27` |
| `KXFEDTWEETS` | 6 | `KXFEDTWEETS-26AUG20` |
| `KXIMFTWEETS` | 6 | `KXIMFTWEETS-26AUG20` |
| `KXWEFTWEETS` | 6 | `KXWEFTWEETS-26AUG20` |

Counts of posts, executive orders, and senate seats — bucketed into ranges that
tile the integers, exactly like temperature. These have never been priced.

## What this changes

The negative result so far is about weather, and weather is the most obvious
partition family on the venue, which is what makes it the most competed. These
seven are the only structurally eligible families that have not been measured,
and several of them settle on a longer horizon than a daily temperature market,
which is a different competitive picture.

It is not a reason for optimism. Low-volume markets are usually wide because
nobody is trading them, and a spread nobody crosses is not an edge — the maker
measurement already showed room that could not be reached. But it is the
difference between "this strategy does not work on this venue" and "this
strategy does not work on the one family everybody watches", and those are not
the same claim.

**When production access returns, price these seven first.** It is a handful of
events, one sweep, and it is the cheapest remaining test of the whole thesis.

## Caveats

* Demo mirrors production's definitions but is not guaranteed identical. Confirm
  the seven exist on production before drawing conclusions from their absence
  or presence.
* One moment, one sweep. Which events are open changes daily, and a family with
  no open event today is invisible here.
* Structure is necessary and nowhere near sufficient. Every one of these still
  needs a reviewer to read its settlement terms before it can qualify anything.
