# Smokemon

[![Docs](https://img.shields.io/badge/docs-mkdocs--material-blue.svg)](https://oovets.github.io/smokemon/)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue.svg)](pyproject.toml)
[![Code style: ruff](https://img.shields.io/badge/code%20style-ruff-261230.svg)](https://github.com/astral-sh/ruff)
[![Core deps: zero](https://img.shields.io/badge/core%20deps-zero%20%28stdlib%29-brightgreen.svg)](pyproject.toml)

> **Observability that remembers only what matters.**
>
> *What if the computer decided what was worth remembering before writing anything to disk?*

Modern observability platforms collect everything.

Every ping, every metric, every log line, every sample. They store millions of data points in the hope that, one day, someone might need them.

Most of those data points describe perfectly healthy systems. The absence of incidents is **not** data worth keeping.

Smokemon starts from a different assumption. Like a smoke detector, Smokemon stays quiet while everything is healthy. It reacts when something deserves attention. Healthy nodes are simply counted. Unhealthy nodes are explained.

> **Healthy systems are not interesting. Failures are.**

Instead of building an ever-growing history of normal operation, Smokemon continuously observes the system in memory and asks a single question:

> **Has something meaningful happened?**

If the answer is **no**, nothing is stored.

If the answer is **yes**, Smokemon captures the incident, the observations that led to it, the recovery, and just enough evidence to explain what happened.

The result is an observability agent that measures continuously but persists selectively.

---

# A different model

Traditional monitoring assumes storage comes first.

```text
Observe ───► Collect ───► Store ───► Aggregate ───► Search ───► Explain
```

Smokemon works a little bit different.

```text
Observe ───► Understand ───► Incident? ───► Capture ───► Remember
                              │
                              └────────────► Forget
```

The incident, **not the metric**, should be the primary unit of information.

---

# Why?

Healthy systems generate an enormous amount of operational data.

Most of it exists only to prove that nothing happened.

Keeping months of healthy ping times, CPU percentages, temperatures, network counters and log entries is expensive, not only in storage, but also in CPU time, memory, write amplification and network traffic.

Smokemon treats normal operation as transient.

A small rolling memory window is enough until something deserves to be remembered.

The goal is not to build the largest historical database.

The goal is to preserve enough evidence to explain failure.

---

# What gets stored?

When an incident is confirmed, Smokemon persists only what is needed to explain it.

- The transition that opened the incident
- The value that triggered it
- The recent baseline
- A bounded pre-incident window
- A bounded set of samples during the incident
- Recovery
- Optional evidence (log excerpts, events, snapshots)

When nothing happens, **nothing is written.**

---

# Design philosophy

Smokemon is built around a few simple principles.

1. Every byte must justify itself.
2. Every sample should answer a question.
3. Collect evidence, not exhaust.
4. Ship conclusions before raw data.
5. Degrade gracefully under resource pressure.
6. Measure the cost of measuring.
7. Every feature must justify its lifetime.

These principles influence every architectural decision from bounded in-memory buffers to append-only incident records.

---

# Built for the edge

Smokemon was designed for environments where observability itself must be inexpensive.

Single-board computers, embedded Linux systems, remote gateways and resource-constrained servers cannot afford an agent that continuously writes to disk, grows databases indefinitely or ships every observation across the network.

Instead, Smokemon keeps its own footprint bounded.

- Memory usage is fixed.
- Disk writes are event-driven.
- Network traffic grows with incidents—not uptime.
- Normal operation lives in memory.
- Only meaningful events become history.

The observer should never become the workload.

---

# What Smokemon is not

Smokemon is **NOT**:

- a metrics database
- a log management platform
- a tracing system
- a dashboard framework
- a Prometheus replacement

Those tools answer questions about *everything* and *anything* and drill down can be time consuming.

Smokemon answers a different question:

> **Why did this system fail?**

---

# Design principles

Smokemon optimizes for understanding rather than retention.

It assumes that the overwhelming majority of operational data has no long-term value. Healthy systems should not fill disks simply to prove that they remained healthy.

Instead of remembering everything, Smokemon remembers what explains failure.

> **Remember the failures - Forget the noise.**
