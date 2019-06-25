# pylint: disable=missing-docstring

from distutils.version import LooseVersion

import nengo
from nengo.builder.signal import Signal
from nengo.builder.operator import ElementwiseInc, DotInc
import numpy as np
import pkg_resources
import pytest
import tensorflow as tf

import nengo_dl
from nengo_dl.tests import dummies


def test_warn_on_opensim_del(Simulator):
    with nengo.Network() as net:
        nengo.Ensemble(10, 1)

    sim = Simulator(net)
    with pytest.warns(RuntimeWarning):
        sim.__del__()
    sim.close()


def test_args(Simulator):
    class Fn:
        def __init__(self):
            self.last_x = None

        def __call__(self, t, x):
            assert t.dtype == x.dtype
            assert t.shape == ()
            assert isinstance(x, np.ndarray)
            assert self.last_x is not x  # x should be a new copy on each call
            self.last_x = x
            assert np.allclose(x[0], t)

    with nengo.Network() as model:
        u = nengo.Node(lambda t: t)
        v = nengo.Node(Fn(), size_in=1, size_out=0)
        nengo.Connection(u, v, synapse=None)

    with Simulator(model) as sim:
        sim.run(0.01)


def test_signal_init_values(Simulator):
    """Tests that initial values are not overwritten."""

    zero = Signal([0.0])
    one = Signal([1.0])
    five = Signal([5.0])
    zeroarray = Signal([[0.0], [0.0], [0.0]])
    array = Signal([1.0, 2.0, 3.0])

    m = nengo.builder.Model(dt=0)
    m.operators += [ElementwiseInc(zero, zero, five),
                    DotInc(zeroarray, one, array)]

    probes = [dummies.Probe(zero, add_to_container=False),
              dummies.Probe(one, add_to_container=False),
              dummies.Probe(five, add_to_container=False),
              dummies.Probe(array, add_to_container=False)]
    m.probes += probes
    for p in probes:
        m.sig[p]['in'] = p.target

    with Simulator(None, model=m) as sim:
        sim.run_steps(3)
        assert np.allclose(sim.data[probes[0]], 0)
        assert np.allclose(sim.data[probes[1]], 1)
        assert np.allclose(sim.data[probes[2]], 5)
        assert np.allclose(sim.data[probes[3]], [1, 2, 3])


def test_entry_point():
    if LooseVersion(tf.__version__) == "1.11.0":
        pytest.xfail("TensorFlow 1.11.0 has conflicting dependencies")

    sims = [ep.load(require=False) for ep in
            pkg_resources.iter_entry_points(group='nengo.backends')]
    assert nengo_dl.Simulator in sims


def test_unconnected_node(Simulator):
    hits = np.array(0)
    dt = 0.001

    def f(_):
        hits[...] += 1

    model = nengo.Network()
    with model:
        nengo.Node(f, size_in=0, size_out=0)
    with Simulator(model, unroll_simulation=1) as sim:
        assert hits == 0
        sim.run(dt)
        assert hits == 1
        sim.run(dt)
        assert hits == 2


@pytest.mark.skipif(LooseVersion(nengo.__version__) <= "2.8.0",
                    reason="Nengo precision option not implemented")
@pytest.mark.parametrize('bits', ["16", "32", "64"])
def test_dtype(Simulator, request, seed, bits):
    # Ensure dtype is set back to default after the test, even if it fails
    default = nengo.rc.get("precision", "bits")
    request.addfinalizer(lambda: nengo.rc.set("precision", "bits", default))

    float_dtype = np.dtype(getattr(np, "float%s" % bits))
    int_dtype = np.dtype(getattr(np, "int%s" % bits))

    with nengo.Network() as model:
        u = nengo.Node([0.5, -0.4])
        a = nengo.Ensemble(10, 2)
        nengo.Connection(u, a)
        nengo.Probe(a)

    nengo.rc.set("precision", "bits", bits)
    with Simulator(model) as sim:
        sim.step()

        # check that the builder has created signals of the correct dtype
        # (note that we may not necessarily use that dtype during simulation)
        for sig in sim.tensor_graph.signals:
            assert sig.dtype in (
                float_dtype, int_dtype), "Signal '%s' wrong dtype" % sig

        # note: we do not check the dtypes of `sim.data` arrays in this
        # version of the test, because those depend on the simulator dtype
        # (which is not controlled by precision.bits)


@pytest.mark.skipif(LooseVersion(nengo.__version__) <= "2.8.0",
                    reason="Nengo Sparse transforms not implemented")
@pytest.mark.parametrize("use_dist", (False, True))
def test_sparse(use_dist, Simulator, rng, seed, monkeypatch):
    # modified version of nengo test_sparse for scipy=False, where we
    # don't expect a warning

    scipy_sparse = pytest.importorskip("scipy.sparse")

    input_d = 4
    output_d = 2
    shape = (output_d, input_d)

    inds = np.asarray(
        [[0, 0],
         [1, 1],
         [0, 2],
         [1, 3]])
    weights = rng.uniform(0.25, 0.75, size=4)
    if use_dist:
        init = nengo.dists.Uniform(0.25, 0.75)
        indices = inds
    else:
        init = scipy_sparse.csr_matrix((weights, inds.T), shape=shape)
        indices = None

    transform = nengo.transforms.Sparse(
        shape, indices=indices, init=init)

    sim_time = 1.
    with nengo.Network(seed=seed) as net:
        x = nengo.processes.WhiteSignal(period=sim_time, high=10,
                                        seed=seed + 1)
        u = nengo.Node(x, size_out=4)
        a = nengo.Ensemble(100, 2)
        conn = nengo.Connection(u, a, synapse=None, transform=transform)
        ap = nengo.Probe(a, synapse=0.03)

    def run_sim():
        with Simulator(net) as sim:
            sim.run(sim_time)
        return sim

    sim = run_sim()

    actual_weights = sim.data[conn].weights

    full_transform = np.zeros(shape)
    full_transform[inds[:, 0], inds[:, 1]] = weights
    if use_dist:
        actual_weights = actual_weights.toarray()
        assert np.array_equal(actual_weights != 0, full_transform != 0)
        full_transform[:] = actual_weights

    conn.transform = full_transform
    with Simulator(net) as ref_sim:
        ref_sim.run(sim_time)

    assert np.allclose(sim.data[ap], ref_sim.data[ap])
