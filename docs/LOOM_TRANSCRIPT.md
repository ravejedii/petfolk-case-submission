# Technical deliverable walkthrough — Loom transcript

This transcript is lightly edited for punctuation and obvious speech-to-text errors.
The content, sequence, and timestamps follow the recorded walkthrough.

> **Post-recording clarification:** In Petfolk's production workflow, an AI-generated
> action would remain a draft until the Regional Partner approves or edits it. The
> approved action would then be written to the clinic's existing Operating Plan
> Tracker. The case artifact uses its local ledger to demonstrate that closed loop.

## Transcript

**0:01**

Hey, Petfolk team. This is the walkthrough of my technical deliverable for the case
study take-home exercise. My biggest takeaway from reading the case is that the
problem Petfolk is facing when it comes to AI and data is not a lack of dashboards.

**0:23**

Leaders receive metrics without a consistent system for deciding what deserves
attention, what actions to take, and whether the execution worked. We talked about
this a little bit in the last few interviews.

**0:38**

I built a six-stage operating loop. The first stages are validating the data and
prioritizing signals.

**0:48**

Both are deterministic tests: they pass or fail, and they do not use AI. Verification
then uses an LLM—an agent with OpenAI—to recommend a specific action based on the data
provided in the CSVs and store the commitment.

**1:06**

Where the loop really closes from a business perspective is that it can use the
following week's data and the scorecard I created to check whether the KPI was met.

**1:27**

I'm going to run the pipeline. I added and loaded the CSVs, and I'm going to accept
the proposed corrections.

**1:37**

There were negative wait-time values, missing values, and lowercase issues. I'm going
to accept those here. Please feel free to inspect the details.

**1:46**

If you have any questions, don't hesitate to reach out. I want to make sure we get
through the whole workflow end to end.

**1:53**

What did we find in the first week? For Mount Pleasant, there was a staff call-out
signal.

**2:06**

Over the last four weeks, it averaged 5.5 call-outs per week versus 1.6. This is
definitely something to watch.

**2:14**

You can see the agent talking conversationally here. This is the admin panel, so it
is slightly different from what Dr. Priya will see in her Monday digest.

**2:23**

I'm going to show that digest in just a moment.

**2:34**

The next step is to run the following week's workflow. It takes the previous week's
data and compares it with the current week.

**2:49**

That lets us see what is happening in the real world. I'm going to scroll up and open
the Monday digest.

**3:06**

Here is the conclusion we want to discuss from a business standpoint. Staff call-outs
worsened in Mount Pleasant in the second week, so the outcome moved in the wrong
direction.

**3:20**

That is important because the week-over-week comparison is dynamic, so it is brought
to the top of Dr. Priya's dashboard.

**3:28**

On a positive note, record completion improved in Morrisville. It is still below the
standard set in the scorecard.

**3:41**

That still needs to be addressed. Even though it came up in the prior week, it needs
to remain near the top of the priorities.

**3:51**

Rather than Dr. Priya spending time in a dashboard or opening spreadsheets, she has
these three priorities in one place.

**3:59**

If she has a question, she can ask the agent using real AI: “What should I do about
Daniel Island?”

**4:10**

Daniel Island was not surfaced in the previous week; it is from the current week.

**4:21**

In the week of May 4, no-shows are elevated compared with the average of 6.6. To
conclude, this is a system that decides what matters, recommends what to do, assigns
accountability, and preserves memory in a ledger.

**4:45**

There are reliable inputs, a translation layer, and outputs. The goal is to let one
leader manage more responsibilities on their plate.

**4:59**

Thank you.
