# S20 Unsafe Optional Alias Repair

R5 finding W003-R5-02 is an implementation defect inside the existing disclosure boundary. An escaping candidate could be discarded while an absent fallback inherited optional-example permission. Existing dangling or cyclic aliases could also appear absent.

The repaired disclosure path preserves unsafe resolution and validates every prefix of each exact expanded candidate before absence handling. Checks use the existing finite budget and filesystem metadata; outside content is not opened. Literal unmatched-glob fallbacks are checked after expansion so bracketed filenames cannot bypass prefix checks. Default behavior for non-disclosure callers remains unchanged.

Complete RED: 60 failures and 16 controls after the isolated root-parent fixture correction. An additional unmatched-glob RED exposed 8 failures with 4 controls. Final focused and canonical coverage: 295 passing tests, including 88 new cases. Source/payload parity and strict package seal pass. All 153 source and 33 governing input pins remained unchanged during canonical evaluation.

The actual three-guide reference classification check passes but reports stale document reviews. This is not Context delivery, HTML, independent-review, W003 or deployment completion. S19 owns actual Context delivery; S08 owns integrated final closure.
