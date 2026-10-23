# Artistic intent safeguards

Technical signals are observations, not verdicts. A possible technique does not make a photograph successful; it prevents a single numeric signal from becoming an unconditional rejection and sends the frame to contextual review.

## Always consider before a negative recommendation

- **Motion blur and panning:** subject motion may carry energy or describe movement. Check whether important structure remains readable and whether the blur is coherent with the scene.
- **Intentional camera movement (ICM):** global softness can be the image-making method. Look for deliberate rhythm, direction, color relationships, and repeated structure.
- **Soft focus:** low edge energy is not enough to prove missed focus. Check the relevant subject at an appropriate scale and distinguish optical softness from accidental focus placement.
- **Low key and silhouettes:** large dark regions can be intentional. Judge separation, retained significant detail, and whether the silhouette reads; overview exposure cannot prove RAW recoverability.
- **High key:** bright backgrounds and clipped-looking previews can be stylistic or caused by the preview rendering. Do not claim irreversible highlight loss from an overview.
- **Grain and noise:** high-frequency energy may be texture, detail, sharpening, or noise. It is not proof of sharpness and not automatic failure.
- **Negative space and minimalism:** low local activity or contrast can be structural. Evaluate relationships and placement, not histogram occupancy alone.
- **Closed eyes:** apply eye-state criteria only when open eyes matter to the moment. A kiss, embrace, prayer, blink between usable neighbors, or expressive gesture needs contextual comparison.
- **Unique moments:** a technically imperfect sole record of a meaningful event remains `review`; it is never removed by a single defect signal.
- **Protected composites:** HDR, panorama, and focus-stack members are required inputs. Their individual exposure or focus differences are evidence for the set, not duplicate failures.

## Evidence discipline

Visual claims must name the inspected `artifact_id`. Region-specific claims must identify a normalized crop inside an observed artifact. If the overview is too small, mark `insufficient_evidence`; do not upgrade metadata or a numeric signal into a visual observation.

The MVP has no automatic reject authority. Outcomes are `keep`, `review`, or `unassessed` proposals until a separately calibrated release gate enables a narrow validated policy.
