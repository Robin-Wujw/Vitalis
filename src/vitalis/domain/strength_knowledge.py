"""Reviewed canonical identities and their movement-family knowledge aliases.

These are exercise-knowledge relationships, not measured muscle activation.
Variant identities stay distinct for longitudinal volume comparisons.
"""

CANONICAL_FAMILIES = {
    'bodyweight_squat': 'squat', 'barbell_squat': 'squat', 'smith_squat': 'squat',
    'bent_over_row': 'row', 'barbell_row': 'row', 'seated_row': 'row',
    'bench_press': 'benchpress', 'dumbbell_bench_press': 'benchpress', 'smith_bench_press': 'benchpress',
    'incline_dumbbell_bench_press': 'benchpress', 'incline_barbell_bench_press': 'benchpress',
    'decline_dumbbell_bench_press': 'benchpress', 'decline_barbell_bench_press': 'benchpress',
    'dumbbell_fly': 'chestfly', 'machine_chest_fly': 'chestfly', 'machine_reverse_fly': 'reversefly',
    'lateral_raise': 'lateralraise', 'dumbbell_lateral_raise': 'lateralraise',
    'lat_pulldown': 'latpulldown', 'straight_arm_pulldown': 'straightarm',
    'deadlift': 'deadlift', 'barbell_deadlift': 'deadlift',
    'push_up': 'pushup', 'kneeling_push_up': 'pushup',
    'overhead_press': 'overheadpress', 'dumbbell_overhead_press': 'overheadpress',
    'pull_up': 'pullup', 'chin_up': 'pullup', 'biceps_curl': 'bicepcurl',
    'dumbbell_biceps_curl': 'bicepcurl', 'barbell_biceps_curl': 'bicepcurl', 'hammer_curl': 'bicepcurl',
    'plank': 'plank', 'barbell_hip_thrust': 'hipthrust', 'weighted_calf_raise': 'calf',
}
KNOWLEDGE_VERSION = 'strength-knowledge-1'
KNOWLEDGE_REFERENCES = (
    'https://www.acefitness.org/resources/everyone/exercise-library/',
    'https://pmc.ncbi.nlm.nih.gov/articles/PMC12965823/',
)
