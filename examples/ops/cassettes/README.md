# Cassettes

A cassette is one recorded session: every completion with the request that
produced it and the answer that came back, and every embedding call with its
texts and vectors. Record one by running a session against a live endpoint with
`oska run --provider openai_compatible --record examples/ops/cassettes/<name>.json`,
then turn it into a scenario script with `oska cassette to-script`. None is
committed yet, because recording one needs a provider this repository does not
carry credentials for; cassettes are meant to be committed once they exist.
