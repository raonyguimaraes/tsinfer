#
# Copyright (C) 2018 University of Oxford
#
# This file is part of tsinfer.
#
# tsinfer is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# tsinfer is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with tsinfer.  If not, see <http://www.gnu.org/licenses/>.
#
"""
Tools for evaluating the algorithm.
"""
import collections
import itertools
import bisect
import random
import json
import logging

import numpy as np
import msprime
import tqdm

import tsinfer.inference as inference
import tsinfer.formats as formats
import tsinfer.constants as constants
import tsinfer.provenance as provenance

logger = logging.getLogger(__name__)


def insert_errors(ts, probability, seed=None):
    """
    Each site has a probability p of generating an error. Errors
    are imposed by choosing a sample and inverting its state with
    a back/recurrent mutation as necessary. Errors resulting in
    fixation of either allele are rejected.

    NOTE: this hasn't been verified and may not have the desired
    statistical properties!
    """
    tables = ts.dump_tables()
    rng = random.Random(seed)
    tables.mutations.clear()
    samples = ts.samples()
    for tree in ts.trees():
        for site in tree.sites():
            assert len(site.mutations) == 1
            mutation_node = site.mutations[0].node
            tables.mutations.add_row(site=site.id, node=mutation_node, derived_state="1")
            for sample in samples:
                # We disallow any fixations. There are two possibilities:
                # (1) We have a singleton and the sample
                # we choose is the mutation node; (2) we have a (n-1)-ton
                # and the sample we choose is on the other root branch.
                if mutation_node == sample:
                    continue
                if {mutation_node, sample} == set(tree.children(tree.root)):
                    continue
                # If the input probability is very high we can still get fixations
                # though by placing a mutation over every sample.
                if rng.random() < probability:
                    # If sample is a descendent of the mutation node we
                    # change the state to 0, otherwise change state to 1.
                    u = sample
                    while u != mutation_node and u != msprime.NULL_NODE:
                        u = tree.parent(u)
                    derived_state = str(int(u == msprime.NULL_NODE))
                    parent = msprime.NULL_MUTATION
                    if u == msprime.NULL_NODE:
                        parent = len(tables.mutations) - 1
                    tables.mutations.add_row(
                        site=site.id, node=sample, parent=parent,
                        derived_state=derived_state)
    return tables.tree_sequence()


def kc_distance(tree1, tree2):
    """
    Returns the Kendall-Colijn topological distance between the specified
    pair of trees. This is a very simple and direct implementation for testing.
    """
    # print(tree1.draw(format="unicode"))
    # print(tree2.draw(format="unicode"))
    samples = tree1.tree_sequence.samples()
    if not np.array_equal(samples, tree2.tree_sequence.samples()):
        raise ValueError("Trees must have the same samples")
    k = samples.shape[0]
    n = (k * (k - 1)) // 2
    M = [np.ones(n + k), np.ones(n + k)]
    for tree_index, tree in enumerate([tree1, tree2]):
        stack = [(tree.root, 0)]
        while len(stack) > 0:
            u, depth = stack.pop()
            children = tree.children(u)
            for v in children:
                stack.append((v, depth + 1))
            for c1, c2 in itertools.combinations(children, 2):
                for u in tree.samples(c1):
                    for v in tree.samples(c2):
                        if u < v:
                            a, b = u, v
                        else:
                            a, b = v, u
                        pair_index = a * (a - 2 * k + 1) // -2 + b - a - 1
                        assert M[tree_index][pair_index] == 1
                        M[tree_index][pair_index] = depth
    return np.linalg.norm(M[0] - M[1])


def tree_pairs(ts1, ts2):
    """
    Returns an iterator over the pairs of trees for each distinct
    interval in the specified pair of tree sequences.
    """
    if ts1.sequence_length != ts2.sequence_length:
        raise ValueError("Tree sequences must be equal length.")
    L = ts1.sequence_length
    trees1 = ts1.trees(sample_lists=True)
    trees2 = ts2.trees(sample_lists=True)
    tree1 = next(trees1)
    tree2 = next(trees2)
    right = 0
    while right != L:
        left = right
        right = min(tree1.interval[1], tree2.interval[1])
        yield (left, right), tree1, tree2
        # Advance
        if tree1.interval[1] == right:
            tree1 = next(trees1, None)
        if tree2.interval[1] == right:
            tree2 = next(trees2, None)


def compare(ts1, ts2):
    """
    Returns the KC distance between the specified tree sequences and
    the intervals over which the trees are compared.
    """
    if not np.array_equal(ts1.samples(), ts2.samples()):
        raise ValueError("Tree sequences must have the same samples")
    breakpoints = [0]
    metrics = []
    for (left, right), tree1, tree2 in tree_pairs(ts1, ts2):
        breakpoints.append(right)
        metrics.append(kc_distance(tree1, tree2))
    return np.array(breakpoints), np.array(metrics)


def strip_singletons(ts):
    """
    Returns a copy of the specified tree sequence with singletons removed.
    """
    tables = ts.dump_tables()
    tables.sites.clear()
    tables.mutations.clear()
    for variant in ts.variants():
        if np.sum(variant.genotypes) > 1:
            site_id = tables.sites.add_row(
                position=variant.site.position,
                ancestral_state=variant.site.ancestral_state,
                metadata=variant.site.metadata)
            assert len(variant.site.mutations) >= 1
            mutation = variant.site.mutations[0]
            parent_id = tables.mutations.add_row(
                site=site_id, node=mutation.node,
                derived_state=mutation.derived_state,
                metadata=mutation.metadata)
            for error in variant.site.mutations[1:]:
                parent = -1
                if error.parent != -1:
                    parent = parent_id
                tables.mutations.add_row(
                    site=site_id, node=error.node, derived_state=error.derived_state,
                    parent=parent, metadata=error.metadata)
    return tables.tree_sequence()


def insert_perfect_mutations(ts, delta=None):
    """
    Returns a copy of the specified tree sequence where the left and right
    coordinates of all edgesets are marked by mutations. This *should* be sufficient
    information to recover the tree sequence exactly.

    This has to be fudged slightly because we cannot have two sites with
    precisely the same coordinates. We work around this by having sites at
    some very small delta from the correct location.
    """
    tables = ts.dump_tables()
    tables.sites.clear()
    tables.mutations.clear()

    num_children = np.zeros(ts.num_nodes, dtype=int)
    parent = np.zeros(ts.num_nodes, dtype=int) - 1

    current_delta = 0
    if delta is not None:
        current_delta = delta

    for (left, right), edges_out, edges_in in ts.edge_diffs():
        last_num_children = list(num_children)
        children_in = set()
        children_out = set()
        parents_in = set()
        parents_out = set()
        for e in edges_out:
            # print("out:", e)
            parent[e.child] = -1
            num_children[e.parent] -= 1
            children_out.add(e.child)
            parents_out.add(e.parent)
        for e in edges_in:
            # print("in:", e)
            parent[e.child] = e.parent
            num_children[e.parent] += 1
            children_in.add(e.child)
            parents_in.add(e.parent)
        root = 0
        while parent[root] != -1:
            root = parent[root]
        # If we have more than 4 edges in the diff, or we have a 2 edge diff
        # that is not a root change this must be a multiple recombination.
        if len(edges_out) > 4 or (len(edges_out) == 2 and root not in parents_in):
            raise ValueError("Multiple recombination detected")
        # We use the value of delta from the previous iteration
        x = left - current_delta
        for u in list(children_out - children_in) + list(children_in & children_out):
            if last_num_children[u] > 0:
                site_id = tables.sites.add_row(position=x, ancestral_state="0")
                tables.mutations.add_row(site=site_id, node=u, derived_state="1")
                x -= current_delta

        # Now update delta for this interval.
        if delta is None:
            max_nodes = 2 * (len(children_out) + len(children_in)) + len(parents_in) + 1
            current_delta = (right - left) / max_nodes
        x = left
        for c in list(children_in - children_out) + list(children_in & children_out):
            if num_children[c] > 0:
                site_id = tables.sites.add_row(position=x, ancestral_state="0")
                tables.mutations.add_row(site=site_id, node=c, derived_state="1")
                x += current_delta

        # It seems wrong that we have to mark every parent, since a few of these
        # will already have been marked out by the children.
        for u in parents_in:
            if parent[u] != -1:
                # print("marking in parent", u, "at", x)
                site_id = tables.sites.add_row(position=x, ancestral_state="0")
                tables.mutations.add_row(site=site_id, node=u, derived_state="1")
                x += current_delta
    tables.sort()
    return tables.tree_sequence()


def get_ancestral_haplotypes(ts):
    """
    Returns a numpy array of the haplotypes of the ancestors in the
    specified tree sequence.
    """
    tables = ts.dump_tables()
    nodes = tables.nodes
    flags = nodes.flags[:]
    flags[:] = 1
    nodes.set_columns(time=nodes.time, flags=flags)

    sites = tables.sites.position
    tsp = tables.tree_sequence()
    B = tsp.genotype_matrix().T

    A = np.zeros((ts.num_nodes, ts.num_sites), dtype=np.uint8)
    A[:] = constants.UNKNOWN_ALLELE
    for edge in ts.edges():
        start = bisect.bisect_left(sites, edge.left)
        end = bisect.bisect_right(sites, edge.right)
        if sites[end - 1] == edge.right:
            end -= 1
        A[edge.parent, start:end] = B[edge.parent, start:end]
    A[:ts.num_samples] = B[:ts.num_samples]
    return A


def get_ancestor_descriptors(A):
    """
    Given an array of ancestral haplotypes A in forwards time-order (i.e.,
    so that A[0] == 0), return the descriptors for each ancestor within
    this set and remove any ancestors that do not have any novel mutations.
    Returns the list of ancestors, the start and end site indexes for
    each ancestor, and the list of focal sites for each one.

    This assumes that the input is SMC safe, and will return incorrect
    results on ancestors that contain trapped genetic material.
    """
    L = A.shape[1]
    ancestors = [np.zeros(L, dtype=np.uint8)]
    focal_sites = [[]]
    start = [0]
    end = [L]
    # ancestors = []
    # focal_sites = []
    # start = []
    # end = []
    mask = np.ones(L)
    for a in A:
        masked = np.logical_and(a == 1, mask).astype(int)
        new_sites = np.where(masked)[0]
        mask[new_sites] = 0
        segment = np.where(a != constants.UNKNOWN_ALLELE)[0]
        # Skip any ancestors that are entirely unknown
        if segment.shape[0] > 0:
            s = segment[0]
            e = segment[-1] + 1
            assert np.all(a[s:e] != constants.UNKNOWN_ALLELE)
            assert np.all(a[:s] == constants.UNKNOWN_ALLELE)
            assert np.all(a[e:] == constants.UNKNOWN_ALLELE)
            ancestors.append(a)
            focal_sites.append(new_sites)
            start.append(s)
            end.append(e)
    return np.array(ancestors, dtype=np.uint8), start, end, focal_sites


def assert_smc(ts):
    """
    Check if the specified tree sequence fulfils SMC requirements. This
    means that we cannot have any discontinuous parent segments.
    """
    parent_intervals = collections.defaultdict(list)
    for es in ts.edgesets():
        parent_intervals[es.parent].append((es.left, es.right))
    for intervals in parent_intervals.values():
        if len(intervals) > 0:
            intervals.sort()
            for j in range(1, len(intervals)):
                if intervals[j - 1][1] != intervals[j][0]:
                    raise ValueError("Only SMC simulations are supported")


def assert_single_recombination(ts):
    """
    Check if the specified tree tree sequence contains only single
    recombination events.
    """
    counter = collections.Counter()
    for e in ts.edgesets():
        counter[e.right] += 1
        if e.right != ts.sequence_length and counter[e.right] > 2:
            raise ValueError("Multiple recombinations at ", e.right)


def build_simulated_ancestors(sample_data, ancestor_data, ts, time_chunking=False):
    # Any non-smc tree sequences are rejected.
    assert_smc(ts)
    assert sample_data.num_inference_sites > 0
    A = get_ancestral_haplotypes(ts)
    # This is all nodes, but we only want the non samples. We also reverse
    # the order to make it forwards time.
    A = A[ts.num_samples:][::-1]
    # We also only want the inference sites
    A = A[:, sample_data.sites_inference[:] == 1]

    # get_ancestor_descriptors ensures that the ultimate ancestor is included.
    ancestors, start, end, focal_sites = get_ancestor_descriptors(A)
    N = len(ancestors)
    if time_chunking:
        time = np.zeros(N)
        intersect_mask = np.zeros(A.shape[1], dtype=int)
        t = 0
        for j in range(N):
            if np.any(intersect_mask[start[j]: end[j]] == 1):
                t += 1
                intersect_mask[:] = 0
            intersect_mask[start[j]: end[j]] = 1
            time[j] = t
    else:
        time = np.arange(N)
    time = -1 * (time - time[-1]) + 1
    for a, s, e, focal, t in zip(ancestors, start, end, focal_sites, time):
        assert np.all(a[:s] == constants.UNKNOWN_ALLELE)
        assert np.all(a[s:e] != constants.UNKNOWN_ALLELE)
        assert np.all(a[e:] == constants.UNKNOWN_ALLELE)
        assert all(s <= site < e for site in focal)
        ancestor_data.add_ancestor(
            start=s, end=e, time=t, focal_sites=np.array(focal, dtype=np.int32),
            haplotype=a[s:e])


def print_tree_pairs(ts1, ts2, compute_distances=True):
    """
    Prints out the trees at each point in the specified tree sequences,
    alone with their KC distance.
    """
    weighted_distance = 0
    total_mismatch_interval = 0
    total_mismatches = 0
    for (left, right), tree1, tree2 in tree_pairs(ts1, ts2):
        print("-" * 20)
        print("Interval          =", left, "--", right)
        print("Source interval   =", tree1.interval)
        print("Inferred interval =", tree2.interval)
        if compute_distances:
            distance = kc_distance(tree1, tree2)
            weighted_distance += (right - left) * distance
            trailer = ""
            if distance != 0:
                total_mismatch_interval += (right - left)
                total_mismatches += 1
                trailer = "[MISMATCH over {:.2f}]".format(right - left)
            print("KC distance       =", distance, trailer)
        print()
        d1 = tree1.draw(format="unicode").splitlines()
        d2 = tree2.draw(format="unicode").splitlines()
        j = 0
        while j < (min(len(d1), len(d2))):
            print(d1[j], " | ", d2[j])
            j += 1
        while j < len(d1):
            print(d1[j], " |")
            j += 1
        while j < len(d2):
            print(" " * len(d1[0]), " | ", d2[j])
            j += 1
        print()
    print("Total weighted tree distance = ", weighted_distance)
    print("Total mismatch interval      = ", total_mismatch_interval)
    print("Total mismatches             = ", total_mismatches)


def subset_sites(ts, position):
    """
    Return a copy of the specified tree sequence with sites reduced to those
    with positions in the specified list.
    """
    tables = ts.dump_tables()
    lookup = frozenset(position)
    tables.sites.clear()
    tables.mutations.clear()
    for site in ts.sites():
        if site.position in lookup:
            site_id = tables.sites.add_row(
                site.position, ancestral_state=site.ancestral_state,
                metadata=site.metadata)
            for mutation in site.mutations:
                tables.mutations.add_row(
                    site_id, node=mutation.node, parent=mutation.parent,
                    derived_state=mutation.derived_state,
                    metadata=mutation.metadata)
    return tables.tree_sequence()


def make_ancestors_ts(samples, ts, remove_leaves=False):
    """
    Return a tree sequence suitable for use as an ancestors tree sequence from the
    specified source tree sequence using the samples in the specified sample
    data. If remove_leaves is True, remove any nodes that are at time zero.

    We generally assume that this is a standard tree sequence output by
    msprime.simulate here.
    """
    position = samples.sites_position[:][samples.sites_inference[:] == 1]
    reduced = subset_sites(ts, position)
    minimised = inference.minimise(reduced)

    tables = minimised.dump_tables()
    # Rewrite the nodes so that 0 is one older than all the other nodes.
    nodes = tables.nodes.copy()
    tables.nodes.clear()
    tables.nodes.add_row(flags=1, time=np.max(nodes.time) + 2)
    tables.nodes.append_columns(
        flags=np.ones_like(nodes.flags),  # Everything is a sample
        time=nodes.time + 1,  # Make sure that all times are > 0
        population=nodes.population,
        individual=nodes.individual, metadata=nodes.metadata,
        metadata_offset=nodes.metadata_offset)
    # Add one to all node references to account for this.
    tables.edges.set_columns(
        left=tables.edges.left,
        right=tables.edges.right,
        parent=tables.edges.parent + 1,
        child=tables.edges.child + 1)
    tables.mutations.set_columns(
        node=tables.mutations.node + 1,
        site=tables.mutations.site,
        parent=tables.mutations.parent,
        derived_state=tables.mutations.derived_state,
        derived_state_offset=tables.mutations.derived_state_offset,
        metadata=tables.mutations.metadata,
        metadata_offset=tables.mutations.metadata_offset)

    trees = minimised.trees()
    tree = next(trees)
    left = 0
    # To simplify things a bit we assume that there's one root. This can
    # violated if we've got no sites at the end of the sequence and get
    # n roots instead.
    root = tree.root
    for tree in trees:
        if tree.root != root:
            tables.edges.add_row(left, tree.interval[0], 0, root + 1)
            root = tree.root
            left = tree.interval[0]
    tables.edges.add_row(left, ts.sequence_length, 0, root + 1)
    tables.sort()
    if remove_leaves:
        # Assume that all leaves are at time 1.
        samples = np.where(tables.nodes.time != 1)[0].astype(np.int32)
        tables.simplify(samples=samples)
    new_ts = tables.tree_sequence()
    return new_ts


def check_ancestors_ts(ts):
    """
    Checks if the specified tree sequence has the required properties for an
    ancestors tree sequence.
    """
    # An empty tree sequence is always fine.
    if ts.num_nodes == 0:
        return
    tables = ts.tables
    if np.any(tables.nodes.time <= 0):
        raise ValueError("All nodes must have time > 0")

    for tree in ts.trees(sample_counts=False):
        # 0 must always be a root and have at least one child.
        if tree.parent(0) != msprime.NULL_NODE:
            raise ValueError("0 is not a root: non null parent")
        if tree.left_child(0) == msprime.NULL_NODE:
            raise ValueError("0 must have at least one child")
        for root in tree.roots:
            if root != 0:
                if tree.left_child(root) != msprime.NULL_NODE:
                    raise ValueError("All non empty subtrees must inherit from 0")
        # Sites must have exactly one mutation
        for site in tree.sites():
            if len(site.mutations) != 1:
                raise ValueError("Sites must have exactly one mutation")


def extract_ancestors(samples, ts):
    """
    Given the specified sample data file and final (unsimplified) tree sequence output
    by tsinfer, return the same tree sequence with the samples removed, which can then
    be used as an ancestors tree sequence.
    """
    position = samples.sites_position[:][samples.sites_inference[:] == 1]
    ts = subset_sites(ts, position)
    tables = ts.dump_tables()

    # The nodes that we want to keep are all those *except* what
    # has been marked as samples.
    samples = np.where(tables.nodes.flags != msprime.NODE_IS_SAMPLE)[0].astype(np.int32)

    # Mark all nodes as samples
    tables.nodes.set_columns(
        flags=np.bitwise_or(tables.nodes.flags, msprime.NODE_IS_SAMPLE),
        time=tables.nodes.time,
        population=tables.nodes.population,
        individual=tables.nodes.individual,
        metadata=tables.nodes.metadata,
        metadata_offset=tables.nodes.metadata_offset)
    # Now simplify down the tables to get rid of all sample edges.
    node_id_map = tables.simplify(
        samples, filter_sites=False, filter_individuals=True, filter_populations=False)

    # We cannot have flags that are both samples and have other flags set,
    # so we need to unset all the sample flags for these.
    flags = np.zeros_like(tables.nodes.flags)
    index = tables.nodes.flags == msprime.NODE_IS_SAMPLE
    flags[index] = msprime.NODE_IS_SAMPLE
    index = tables.nodes.flags != msprime.NODE_IS_SAMPLE
    flags[index] = np.bitwise_and(tables.nodes.flags[index], ~msprime.NODE_IS_SAMPLE)

    tables.nodes.set_columns(
        flags=flags,
        time=tables.nodes.time,
        population=tables.nodes.population,
        individual=tables.nodes.individual,
        metadata=tables.nodes.metadata,
        metadata_offset=tables.nodes.metadata_offset)
    # Drop site metadata and set the ancestral_state to zeros
    tables.sites.set_columns(
        position=tables.sites.position,
        ancestral_state=np.zeros(len(tables.sites), dtype=np.int8) + ord('0'),
        ancestral_state_offset=np.arange(len(tables.sites) + 1, dtype=np.uint32))

    # Drop mutation metadata and set the derived_state to ones
    tables.mutations.set_columns(
        site=tables.mutations.site,
        node=tables.mutations.node,
        derived_state=np.zeros(len(tables.mutations), dtype=np.int8) + ord('1'),
        derived_state_offset=np.arange(len(tables.mutations) + 1, dtype=np.uint32))

    record = provenance.get_provenance_dict(command="extract_ancestors")
    tables.provenances.add_row(record=json.dumps(record))

    return tables, node_id_map


def insert_srb_ancestors(samples, ts, show_progress=False):
    """
    Given the specified sample data file and final (unsimplified) tree sequence output
    by tsinfer, return a tree sequence with an ancestor inserted for each shared
    recombination breakpoint resulting from the sample edges. The returned tree
    sequence can be used as an ancestors tree sequence.
    """
    logger.info("Starting srb ancestor insertion")
    tables = ts.dump_tables()
    edges = tables.edges
    # In lexsort the primary sort key is *last*
    index = np.lexsort((edges.left, edges.child))
    logger.info("Sorted edges")
    flags = tables.nodes.flags

    # Definitely possible to do this more efficiently with numpy, but may not be
    # worth it.
    srb_index = {}
    last_edge = edges[index[0]]
    progress = tqdm.tqdm(
        total=len(edges) - 1, desc="scan edges", disable=not show_progress)
    for j in index[1:]:
        progress.update()
        edge = edges[j]
        condition = (
            flags[edge.child] == msprime.NODE_IS_SAMPLE and
            edge.child == last_edge.child and
            edge.left == last_edge.right)
        if condition:
            key = edge.left, last_edge.parent, edge.parent
            if key in srb_index:
                count, left_bound, right_bound = srb_index[key]
                srb_index[key] = (
                    count + 1,
                    max(left_bound, last_edge.left),
                    min(right_bound, edge.right))
            else:
                srb_index[key] = 1, last_edge.left, edge.right
        last_edge = edge
    progress.close()

    logger.info("Built SRB map with {} items".format(len(srb_index)))
    tables, node_id_map = extract_ancestors(samples, ts)
    logger.info("Extracted ancestors ts")
    time = tables.nodes.time

    num_extra = 0
    progress = tqdm.tqdm(
        total=len(srb_index), desc="scan index", disable=not show_progress)
    for k, v in srb_index.items():
        progress.update()
        if v[0] > 1:
            left, right = v[1:]
            x, pl, pr = k
            pl = node_id_map[pl]
            pr = node_id_map[pr]
            t = min(time[pl], time[pr]) - 1e-4
            node = tables.nodes.add_row(flags=constants.NODE_IS_SRB_ANCESTOR, time=t)
            tables.edges.add_row(left, x, pl, node)
            tables.edges.add_row(x, right, pr, node)
            num_extra += 1
    progress.close()

    logger.info("Generated {} extra ancestors".format(num_extra))
    tables.sort()
    ancestors_ts = tables.tree_sequence()
    return ancestors_ts


def run_perfect_inference(
        base_ts, num_threads=1, path_compression=False,
        extended_checks=True, time_chunking=True, progress_monitor=None,
        use_ts=False, engine=constants.C_ENGINE):
    """
    Runs the perfect inference process on the specified tree sequence.
    """
    ts = insert_perfect_mutations(base_ts)
    sample_data = formats.SampleData.from_tree_sequence(ts)

    if use_ts:
        # Use the actual tree sequenc that was provided as the basis for copying.
        ancestors_ts = make_ancestors_ts(sample_data, ts, remove_leaves=True)
    else:
        ancestor_data = formats.AncestorData(sample_data)
        build_simulated_ancestors(
            sample_data, ancestor_data, ts, time_chunking=time_chunking)
        ancestor_data.finalise()

        ancestors_ts = inference.match_ancestors(
            sample_data, ancestor_data, engine=engine, path_compression=path_compression,
            num_threads=num_threads, extended_checks=extended_checks,
            progress_monitor=progress_monitor)
    # If time_chunking is turned on we need to stabilise the node ordering in the output
    # to ensure that the node IDs are comparable.
    inferred_ts = inference.match_samples(
        sample_data, ancestors_ts, engine=engine, path_compression=path_compression,
        num_threads=num_threads, extended_checks=extended_checks,
        progress_monitor=progress_monitor,
        stabilise_node_ordering=time_chunking and not path_compression)
    return ts, inferred_ts


def count_sample_child_edges(ts):
    """
    Returns an array counting the number of edges where each sample is a child.
    The returned array is of length num_samples, i.e., is indexed by the
    sample index not by its ID.
    """
    child_counts = np.bincount(ts.tables.edges.child)
    return child_counts[ts.samples()]
# def mean_sample_ancestry(ts):
#     """
#     Computes the mean sample ancestry for each node in the tree sequence. This is
#     defined as the fraction of samples below a given node from each population,
#     averaged along the length of sequence that the node is ancestral to at
#     least one sample.
#     """
