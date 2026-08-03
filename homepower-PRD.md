# 635 Central Home Power Monitor

I would like to set up (possibly build) a system for collecting, visualizing, and analyzing home power use.

The result should be something similar to
[agentsview](https://www.agentsview.io:), an EDA dashboard to better
understand power usage and cost, backed by a passive collection system that gathers
and stores telemetry. It should be a lightweight, simple
system running on my `studio` home machine.


## Inputs

- Tesla car
- PG&E data via Smart Panel and Rainforest Eagle 3 (purchased, not arrived or installed yet)
- Main house solar -- SolarEdge inverter
- ADU solar -- Enphase inverter

I asked Gemini to do a first pass analyzing my power setup by looking at a years worth of bills, and it produced `635-central-energy-analysis-gemini.md`.

## Technology choices

If there are existing open source projects to build or use that'd be great. But
I'm also not against building and maintaining something bespoke.

If building is the choice, then:

- My stack of choice is python/django
- I'm skeptical I need a full tsdb, maybe sqlite would be sufficient?
