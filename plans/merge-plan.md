Okay, now take a look at
* https://github.com/sifferman/ternary_matmul
* https://github.com/sifferman/ternip
Now, I want to make a plan for how to neatly merge things in.

This "ternip_claude_churner" investigation has been extremely insightful. We've gone from ~100 tok/s to ~2600 tok/s. However, the code base has gotten very disoragnized in the flurry of research. It'll be non-trivial to merge everything in.

First, take a deep look at the diff to understand what was done.

Then make any final changes:
* If a line has been modified, does it need to be modified?
* See if anything disobeys STYLE.md
* Is anything inconsistent or excessive?


Let's create a new set of commits that are completely unrelated to the true order and research done in this repo. They should be much cleaner. For example, "added pipeline stage in mul_star" or "removed outdated code" or "fixed rms_norm bug with multiple batch sizes" or something like that.
If there are a lot of small formatting changes, they should probably just go in a formatting commit, or they should be ignored altogether. Removing lines of code is generally always okay, but adding or modifications should be heavily reconsidered.

The goal is to make ~10 or so commits that break down the true progress that NumSeparateKernels introduces. An impatient reviewer in a bad mood should be able to look at the ~4-8 word commit message, and look through the changes, and everything should be extremely obvious why it was added that way

Put these commits in a new branch called tps_improvement
