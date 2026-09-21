import crypto from 'node:crypto';

import { makeEdge, walkPreOrder } from './graph_primitives.mjs';

const CALLABLE_KINDS = new Set(['function', 'method']);
const RESOURCE_KINDS = new Set(['reads', 'writes', 'enqueues', 'publishes', 'consumes']);
const MAX_BUCKET_SIZE = 200;
const LARGE_BUCKET_ANCHOR_NGRAMS = 16;
const LARGE_BUCKET_NEIGHBOR_WINDOW = 8;
// Bottom-k sketch width. Small on purpose: the sketch is persisted on every
// callable node and shipped back through the extractor on every reindex, and a
// hash-ordered sample of this size still estimates overlap accurately.
const MAX_NGRAMS = 128;
const MIN_CONTAINMENT_NGRAMS = 32;
// Floor on the cutoff-restricted sketch, not on the true n-gram count. The
// restricted sketch holds about MAX_NGRAMS / size-ratio entries, so this is
// effectively the largest ratio at which containment stays estimable: 8
// admits roughly a 16x difference, against the 4x the unsampled floor allowed.
const MIN_CONTAINMENT_SAMPLE = 8;
const MIN_SCORE = 0.82;
// Bounds the edge set: without it a directory of near-duplicates emits one edge
// per pair, which is quadratic in the bucket and dominates the whole index.
const MAX_EDGES_PER_NODE = 10;
const PROFILE_ALGORITHM = 'typescript_syntax_kind_hashes_v6';

function strings(value) {
  return Array.isArray(value) ? value.filter((item) => typeof item === 'string') : [];
}

function stringMap(value) {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return new Map();
  return new Map(
    Object.entries(value).filter(
      ([key, item]) => typeof key === 'string' && typeof item === 'string'
    )
  );
}

function uint32s(value) {
  return Array.isArray(value)
    ? value.filter(
        (item) => Number.isInteger(item) && item >= 0 && item <= 0xffffffff
      )
    : [];
}

function overlap(left, right) {
  let intersection = 0;
  for (const value of left) {
    if (right.has(value)) intersection += 1;
  }
  return intersection;
}

function jaccard(left, right) {
  if (left.size === 0 && right.size === 0) return 1;
  if (left.size === 0 || right.size === 0) return 0;
  const intersection = overlap(left, right);
  return intersection / (left.size + right.size - intersection);
}

function directoryBucket(path) {
  const normalized = typeof path === 'string' ? path.replaceAll('\\', '/') : '';
  const separator = normalized.lastIndexOf('/');
  return separator >= 0 ? normalized.slice(0, separator) || '.' : '.';
}

function normalizedSyntaxTokens(ts, root) {
  const tokens = [];
  walkPreOrder(ts, root, (node) => {
    if (
      ts.isStringLiteral(node) ||
      ts.isNoSubstitutionTemplateLiteral(node) ||
      ts.isNumericLiteral(node) ||
      node.kind === ts.SyntaxKind.TrueKeyword ||
      node.kind === ts.SyntaxKind.FalseKeyword ||
      node.kind === ts.SyntaxKind.NullKeyword
    ) {
      tokens.push('Literal');
    } else if (ts.isIdentifier(node)) {
      tokens.push('Identifier');
    } else {
      tokens.push(ts.SyntaxKind[node.kind] || `Kind${node.kind}`);
    }
  });
  return tokens;
}

// FNV-1a. Deterministic across processes, platforms and locales, unlike a sort
// on the raw n-gram text.
function ngramHash(value) {
  let hash = 2166136261;
  for (let index = 0; index < value.length; index += 1) {
    hash ^= value.charCodeAt(index);
    hash = Math.imul(hash, 16777619) >>> 0;
  }
  return hash >>> 0;
}

// Bottom-k by hash rather than lexicographically. A lexicographic prefix is a
// biased sample: a large function keeps only its alphabetically-smallest
// n-grams, so a small function contained within it falls outside the window and
// the containment signal is lost. Hash order samples the same sub-space on both
// sides, so overlap survives truncation.
function ngramSketch(tokens) {
  const values = new Set();
  for (let index = 0; index <= tokens.length - 3; index += 1) {
    values.add(ngramHash(tokens.slice(index, index + 3).join(' ')));
  }
  return {
    total: values.size,
    sketch: [...values].sort((left, right) => left - right).slice(0, MAX_NGRAMS),
  };
}

// Elements above the narrower sketch's cutoff were never sampled on that side,
// so comparing them would understate the overlap. Restrict both sides to the
// sub-space both actually sampled.
function sketchCutoff(profile) {
  const sketch = profile.hashes;
  if (!Number.isInteger(profile.ngram_count) || profile.ngram_count <= sketch.length) {
    return Infinity;
  }
  let cutoff = 0;
  for (const value of sketch) cutoff = Math.max(cutoff, value);
  return cutoff;
}

function restrictToCutoff(sketch, cutoff) {
  if (cutoff === Infinity) return new Set(sketch);
  return new Set(sketch.filter((value) => value <= cutoff));
}

function edgeFeatures(edges) {
  const callTargets = new Map();
  const resourceTargets = new Map();
  function add(targets, source, target) {
    if (!targets.has(source)) targets.set(source, new Set());
    targets.get(source).add(target);
  }
  for (const edge of edges) {
    if (edge.kind === 'calls') add(callTargets, edge.source, edge.target);
    else if (RESOURCE_KINDS.has(edge.kind)) {
      add(resourceTargets, edge.source, edge.target);
    }
  }
  return { callTargets, resourceTargets };
}

function parameterCount(ts, syntaxNode) {
  if (Array.isArray(syntaxNode.parameters)) return syntaxNode.parameters.length;
  if (ts.isVariableDeclaration(syntaxNode)) {
    return Array.isArray(syntaxNode.initializer?.parameters)
      ? syntaxNode.initializer.parameters.length
      : 0;
  }
  return 0;
}

function isAsync(ts, syntaxNode) {
  const target = ts.isVariableDeclaration(syntaxNode)
    ? syntaxNode.initializer
    : syntaxNode;
  return Boolean(
    target &&
      ts.getModifiers?.(target)?.some(
        (modifier) => modifier.kind === ts.SyntaxKind.AsyncKeyword
      )
  );
}

function currentProfiles(ts, declarations, edges) {
  const features = edgeFeatures(edges);
  const profiles = [];
  for (const node of declarations.nodes) {
    if (!CALLABLE_KINDS.has(node.kind)) continue;
    const syntax = declarations.syntaxById.get(node.id);
    if (!syntax) continue;
    const tokens = normalizedSyntaxTokens(ts, syntax.syntaxNode);
    const { total, sketch } = ngramSketch(tokens);
    if (total < 3) continue;
    const callTargets = [...(features.callTargets.get(node.id) || [])].sort();
    const callTargetModules = Object.fromEntries(
      callTargets.flatMap((target) => {
        if (target.startsWith('mod:')) return [[target, target.slice(4)]];
        const module = declarations.modulesByDeclarationId.get(target);
        return module ? [[target, module]] : [];
      })
    );
    const profile = {
      algorithm: PROFILE_ALGORITHM,
      bucket: [
        directoryBucket(node.path),
        node.kind,
        parameterCount(ts, syntax.syntaxNode),
        isAsync(ts, syntax.syntaxNode) ? 'async' : 'sync',
      ].join('|'),
      structure_hash: crypto
        .createHash('sha256')
        .update(tokens.join(' '))
        .digest('hex')
        .slice(0, 16),
      ngram_hashes: sketch,
      ngram_count: total,
      call_targets: callTargets,
      call_target_modules: callTargetModules,
      resource_targets: [...(features.resourceTargets.get(node.id) || [])].sort(),
    };
    node.properties.similarity = profile;
    profiles.push({
      id: node.id,
      path: node.path,
      start_line: node.start_line,
      end_line: node.end_line,
      ...profile,
      hashes: sketch,
    });
  }
  return profiles;
}

function restoredCallFeatures(values, modules, validTargetIds, moduleNames) {
  const recordedModules = stringMap(modules);
  const restored = new Set();
  const restoredModules = {};
  for (const target of strings(values)) {
    if (
      validTargetIds === null ||
      validTargetIds.has(target) ||
      target.startsWith('ext:') ||
      target.startsWith('extsym:')
    ) {
      restored.add(target);
      const module = recordedModules.get(target);
      if (module) restoredModules[target] = module;
      continue;
    }
    const module = recordedModules.get(target);
    const fallback = module ? `mod:${module}` : null;
    if (fallback && moduleNames.has(module)) {
      restored.add(fallback);
      restoredModules[fallback] = module;
    }
  }
  return {
    targets: [...restored].sort(),
    modules: restoredModules,
  };
}

function restoredFeatureTargets(values, validTargetIds) {
  if (validTargetIds === null) return strings(values);
  return strings(values).filter(
    (target) =>
      validTargetIds.has(target) ||
      target.startsWith('ext:') ||
      target.startsWith('extsym:')
  );
}

function restoredProfiles(rows, options = {}) {
  const validTargetIds = Array.isArray(options.validTargetIds)
    ? new Set(options.validTargetIds.filter((value) => typeof value === 'string'))
    : null;
  const moduleNames = new Set(strings(options.moduleNames));
  const profiles = [];
  for (const row of rows || []) {
    const profile = row?.profile;
    if (
      !row ||
      typeof row.id !== 'string' ||
      !profile ||
      profile.algorithm !== PROFILE_ALGORITHM
    ) {
      continue;
    }
    const profileHashes = uint32s(profile.ngram_hashes);
    if (profileHashes.length < 3) continue;
    const callFeatures = restoredCallFeatures(
      profile.call_targets,
      profile.call_target_modules,
      validTargetIds,
      moduleNames
    );
    profiles.push({
      id: row.id,
      path: typeof row.path === 'string' ? row.path : null,
      start_line: Number.isInteger(row.start_line) ? row.start_line : null,
      end_line: Number.isInteger(row.end_line) ? row.end_line : null,
      algorithm: PROFILE_ALGORITHM,
      bucket: String(profile.bucket || ''),
      structure_hash: String(profile.structure_hash || ''),
      hashes: profileHashes,
      ngram_count: Number.isInteger(profile.ngram_count)
        ? profile.ngram_count
        : profileHashes.length,
      call_targets: callFeatures.targets,
      call_target_modules: callFeatures.modules,
      resource_targets: restoredFeatureTargets(profile.resource_targets, validTargetIds),
    });
  }
  return profiles;
}

// Sets are built once per profile, not once per pair: the scoring loop is
// quadratic in the bucket, so rebuilding them inside it dominated its cost.
function withScoringSets(profile) {
  profile.cutoff = sketchCutoff(profile);
  profile.callSet = new Set(profile.call_targets);
  profile.resourceSet = new Set(profile.resource_targets);
  profile.fullNgramSet = new Set(profile.hashes);
  return profile;
}

function scoreProfiles(source, target) {
  const cutoff = Math.min(source.cutoff, target.cutoff);
  const sourceNgrams =
    cutoff === Infinity ? source.fullNgramSet : restrictToCutoff(source.hashes, cutoff);
  const targetNgrams =
    cutoff === Infinity ? target.fullNgramSet : restrictToCutoff(target.hashes, cutoff);
  const tokenIntersection = overlap(sourceNgrams, targetNgrams);
  const tokenJaccard =
    sourceNgrams.size === 0 && targetNgrams.size === 0
      ? 1
      : tokenIntersection /
        (sourceNgrams.size + targetNgrams.size - tokenIntersection);
  const sourceIsSmaller = source.ngram_count <= target.ngram_count;
  const smallerNgrams = sourceIsSmaller ? sourceNgrams : targetNgrams;
  const smallerProfileSize = sourceIsSmaller
    ? source.ngram_count
    : target.ngram_count;
  // A bottom-k containment ratio is meaningful only when the shared cutoff
  // leaves enough observations from the truly smaller profile. Gating on the
  // unsampled count allowed one shared n-gram out of a 1-3 item restricted
  // sketch to claim perfect containment.
  //
  // The restricted sketch shrinks in proportion to the size ratio, so reusing
  // the unsampled floor here made containment switch off abruptly instead of
  // degrading: a helper contained in a host twice its size scored 1.00, while
  // the same helper against a host seven times its size fell under the floor,
  // scored on Jaccard alone, and emitted no edge at all -- exactly the case
  // containment exists to catch. The true-count floor above decides whether
  // the pair is big enough to compare; this one only needs to keep the
  // sampling-rate estimator out of single-digit-observation territory.
  // Gate on the restricted sample size, which is what makes the estimate
  // reliable. Gating on the intersection instead is self-defeating: when the
  // restricted sample is entirely shared they are the same number, so a
  // 4-of-4 sample certifies itself and reports perfect containment off four
  // observations. The restricted sample holds roughly MAX_NGRAMS / size-ratio
  // entries, so this floor is also what bounds how far apart two profiles can
  // be before containment stops being estimable at all.
  const containmentEligible =
    smallerProfileSize >= MIN_CONTAINMENT_NGRAMS &&
    smallerNgrams.size >= MIN_CONTAINMENT_SAMPLE;
  let tokenContainment = 0;
  if (containmentEligible) {
    const samplingRate = smallerNgrams.size / smallerProfileSize;
    const estimatedIntersection = tokenIntersection / samplingRate;
    tokenContainment = Math.min(1, estimatedIntersection / smallerProfileSize);
  }
  const callScore = jaccard(source.callSet, target.callSet);
  const resourceScore = jaccard(source.resourceSet, target.resourceSet);
  let score = Math.max(tokenJaccard, tokenContainment);
  const reasons = [`token_jaccard=${tokenJaccard.toFixed(2)}`];
  if (containmentEligible) {
    reasons.push(`token_containment=${tokenContainment.toFixed(2)}`);
    reasons.push(`token_containment_sample=${smallerNgrams.size}`);
  }
  if (source.structure_hash === target.structure_hash) {
    score = Math.max(score, 0.95);
    reasons.push('same_structure');
  }
  if (source.call_targets.length > 0 || target.call_targets.length > 0) {
    score += (1 - score) * 0.15 * callScore;
    reasons.push(`call_overlap=${callScore.toFixed(2)}`);
  }
  if (source.resource_targets.length > 0 || target.resource_targets.length > 0) {
    score += (1 - score) * 0.15 * resourceScore;
    reasons.push(`resource_overlap=${resourceScore.toFixed(2)}`);
  }
  return { score: Math.round(score * 10000) / 10000, reasons };
}

// Deterministic seam for testing the sketch itself without constructing a
// TypeScript Program. The production path feeds this scorer syntax-kind tokens
// from `normalizedSyntaxTokens`; keeping the seam here exercises the same
// bottom-k sampling, cutoff and containment implementation.
export function scoreNormalizedTokenSequences(leftTokens, rightTokens) {
  function profile(tokens) {
    const { total, sketch } = ngramSketch(tokens);
    return withScoringSets({
      hashes: sketch,
      ngram_count: total,
      call_targets: [],
      resource_targets: [],
      structure_hash: crypto
        .createHash('sha256')
        .update(tokens.join('\u0000'))
        .digest('hex')
        .slice(0, 16),
    });
  }
  const left = profile(leftTokens);
  const right = profile(rightTokens);
  return {
    left_ngram_count: left.ngram_count,
    left_sketch_count: left.hashes.length,
    right_ngram_count: right.ngram_count,
    right_sketch_count: right.hashes.length,
    ...scoreProfiles(left, right),
  };
}

function similarityEdge(source, target, score, reasons) {
  return makeEdge(
    source.id,
    target.id,
    'similar_to',
    {
      kind: 'ast_similarity',
      path: source.path,
      start_line: source.start_line,
      end_line: source.end_line,
      detail: `score=${score}; ${reasons.join('; ')}`,
    },
    'inferred',
    {
      score,
      reasons,
      bucket: source.bucket,
      algorithm: PROFILE_ALGORITHM,
      source_structure_hash: source.structure_hash,
      target_structure_hash: target.structure_hash,
    },
    { status: 'resolved', strategy: PROFILE_ALGORITHM, fallbacks: [] }
  );
}

function orderedProfilePair(left, right) {
  return left.id < right.id ? [left, right] : [right, left];
}

function exhaustiveCandidatePairs(profiles) {
  const pairs = [];
  for (let index = 0; index < profiles.length; index += 1) {
    for (let targetIndex = index + 1; targetIndex < profiles.length; targetIndex += 1) {
      pairs.push(orderedProfilePair(profiles[index], profiles[targetIndex]));
    }
  }
  return pairs;
}

// Large buckets use the lowest-hash n-grams as deterministic locality anchors.
// Within each posting list, a fixed neighbour window gives every profile useful
// candidates while keeping work linear in the number of persisted sketch items.
function approximateCandidatePairs(profiles) {
  const byAnchor = new Map();
  for (const profile of profiles) {
    for (const anchor of profile.hashes.slice(0, LARGE_BUCKET_ANCHOR_NGRAMS)) {
      if (!byAnchor.has(anchor)) byAnchor.set(anchor, []);
      byAnchor.get(anchor).push(profile);
    }
  }

  const pairs = new Map();
  for (const anchoredProfiles of byAnchor.values()) {
    anchoredProfiles.sort((left, right) =>
      left.id < right.id ? -1 : left.id > right.id ? 1 : 0
    );
    for (let index = 0; index < anchoredProfiles.length; index += 1) {
      const stop = Math.min(
        anchoredProfiles.length,
        index + LARGE_BUCKET_NEIGHBOR_WINDOW + 1
      );
      for (let targetIndex = index + 1; targetIndex < stop; targetIndex += 1) {
        const [left, right] = orderedProfilePair(
          anchoredProfiles[index],
          anchoredProfiles[targetIndex]
        );
        pairs.set(`${left.id}\u0000${right.id}`, [left, right]);
      }
    }
  }
  return [...pairs.values()];
}

function persistedProfileRow(profile) {
  return {
    id: profile.id,
    path: profile.path,
    start_line: profile.start_line,
    end_line: profile.end_line,
    profile: {
      algorithm: PROFILE_ALGORITHM,
      bucket: profile.bucket,
      structure_hash: profile.structure_hash,
      ngram_hashes: profile.hashes,
      ngram_count: profile.ngram_count,
      call_targets: [...profile.call_targets],
      call_target_modules: { ...profile.call_target_modules },
      resource_targets: [...profile.resource_targets],
    },
  };
}

function scorePreparedProfiles(allProfiles) {
  const byBucket = new Map();
  for (const profile of allProfiles) {
    withScoringSets(profile);
    if (!byBucket.has(profile.bucket)) byBucket.set(profile.bucket, []);
    byBucket.get(profile.bucket).push(profile);
  }

  const emitted = new Map();
  const warnings = [];
  for (const [bucket, profiles] of byBucket) {
    const approximated = profiles.length > MAX_BUCKET_SIZE;
    const candidatePairs = approximated
      ? approximateCandidatePairs(profiles)
      : exhaustiveCandidatePairs(profiles);
    if (approximated) {
      const exhaustivePairCount = (profiles.length * (profiles.length - 1)) / 2;
      warnings.push({
        kind: 'similarity_bucket_approximated',
        message: `Approximated TypeScript similarity bucket ${JSON.stringify(bucket)} with ${profiles.length} candidates using ${candidatePairs.length} bounded bottom-k neighbour pair(s) instead of ${exhaustivePairCount} exhaustive pair(s).`,
      });
    }

    // Incremental reindex replaces the complete TypeScript similarity edge set,
    // so restored/restored pairs must be emitted as well as pairs touching a
    // callable parsed in this extractor invocation.
    for (const [left, right] of candidatePairs) {
      // Overload sets and declaration/implementation pairs put two entries with
      // one id in `declarations.nodes`; a node is not similar to itself.
      if (left.id === right.id) continue;
      const { score, reasons } = scoreProfiles(left, right);
      if (score < MIN_SCORE) continue;
      const [source, target] = orderedProfilePair(left, right);
      const key = `${source.id}\u0000${target.id}`;
      const existing = emitted.get(key);
      if (!existing || score > existing.properties.score) {
        emitted.set(key, similarityEdge(source, target, score, reasons));
      }
    }
  }

  // Keep each node's strongest neighbours only. Without this a directory of
  // near-duplicates emits one edge per pair, and the edge set - not the source
  // it describes - becomes the bulk of the index.
  const rankedByNode = new Map();
  for (const edge of emitted.values()) {
    for (const nodeId of [edge.source, edge.target]) {
      if (!rankedByNode.has(nodeId)) rankedByNode.set(nodeId, []);
      rankedByNode.get(nodeId).push(edge);
    }
  }
  const kept = new Set();
  for (const nodeEdges of rankedByNode.values()) {
    nodeEdges.sort(
      (left, right) =>
        right.properties.score - left.properties.score || compareEdgeKey(left, right)
    );
    for (const edge of nodeEdges.slice(0, MAX_EDGES_PER_NODE)) kept.add(edge);
  }
  const droppedByBucket = new Map();
  for (const edge of emitted.values()) {
    if (kept.has(edge)) continue;
    const bucket = String(edge.properties.bucket || '');
    droppedByBucket.set(bucket, (droppedByBucket.get(bucket) || 0) + 1);
  }
  for (const [bucket, droppedEdges] of droppedByBucket) {
    warnings.push({
      kind: 'similarity_edges_capped',
      message: `Kept edges in TypeScript similarity bucket ${JSON.stringify(bucket)} ranking among the ${MAX_EDGES_PER_NODE} strongest neighbours of at least one endpoint; omitted ${droppedEdges} lower-scoring edge(s).`,
    });
  }

  return {
    // Codepoint order, not locale collation: the output must not depend on the
    // host's ICU build or LANG.
    edges: [...kept].sort(compareEdgeKey),
    warnings,
  };
}

export function scorePersistedSimilarityProfiles(rows, options = {}) {
  const profiles = restoredProfiles(rows, options);
  return {
    ...scorePreparedProfiles(profiles),
    profiles: profiles.map(persistedProfileRow),
  };
}

export function collectSimilarity(
  ts,
  declarations,
  edges,
  existingRows = [],
  options = {}
) {
  const current = currentProfiles(ts, declarations, edges);
  const currentRows = current.map(persistedProfileRow);
  if (options.deferScoring === true) {
    return { edges: [], warnings: [], profiles: currentRows };
  }
  const restoredAll = restoredProfiles(existingRows);
  const currentIds = new Set(current.map((profile) => profile.id));
  const restored = restoredAll.filter((profile) => !currentIds.has(profile.id));
  return {
    ...scorePreparedProfiles([...restored, ...current]),
    profiles: currentRows,
  };
}

function compareEdgeKey(left, right) {
  const leftKey = `${left.source}\u0000${left.target}`;
  const rightKey = `${right.source}\u0000${right.target}`;
  return leftKey < rightKey ? -1 : leftKey > rightKey ? 1 : 0;
}
