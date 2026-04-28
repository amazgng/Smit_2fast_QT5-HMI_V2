# Speed Decode Fix 2

Field test showed the loom actual speed was about 200 rpm while the dashboard displayed about 504 rpm.
The prior fix incorrectly treated a speed byte pair such as `C5 00` / `C8 00` as a big-endian word and then scaled it by 100.

This version changes speed decoding:

- `C8 00` is decoded as `200 rpm`, not `512.0 rpm`.
- `C5 00` is decoded as `197 rpm`, not `504.3 rpm`.
- `[speed, 00]` and `[00, speed]` compact reply formats are treated as direct rpm values when the speed is in a realistic loom range.
- Big-endian word and x100 scaling are still supported for other loom firmware variants.

The same byte-pair rule is applied to:

- dedicated speed command replies
- complete-status speed field
- full-status speed field

The backend also returns `speed_decode` in the speed command result so raw byte interpretation can be checked during field testing.
