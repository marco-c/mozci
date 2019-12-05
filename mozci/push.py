from argparse import Namespace
from collections import defaultdict

import requests
from adr.errors import MissingDataError
from adr.query import run_query
from adr.util.memoize import memoize, memoized_property
from loguru import logger

from mozci.task import (
    LabelSummary,
    Status,
    Task,
)

HGMO_JSON_URL = "https://hg.mozilla.org/integration/{branch}/rev/{rev}?style=json"
HGMO_JSON_PUSHES_URL = "https://hg.mozilla.org/integration/{branch}/json-pushes?version=2&startID={push_id_start}&endID={push_id_end}"  # noqa
TASKGRAPH_ARTIFACT_URL = "https://firefox-ci-tc.services.mozilla.com/api/index/v1/task/gecko.v2.autoland.revision.{rev}.taskgraph.decision/artifacts/public/{artifact}"  # noqa
SHADOW_SCHEDULER_ARTIFACT_URL = "https://firefox-ci-tc.services.mozilla.com/api/index/v1/task/gecko.v2.autoland.revision.{rev}.source/shadow-scheduler-{name}/artifacts/public/shadow-scheduler/optimized_tasks.list"  # noqa

# The maximum number of parents or children to look for previous/next task runs,
# when the task did not run on the currently considered push.
MAX_DEPTH = 14


class Push:

    def __init__(self, revs, branch='autoland'):
        """A representation of a single push.

        Args:
            revs (list): List of revisions of commits in the push (top-most is the first element).
            branch (str): Branch to look on (default: autoland).
        """
        if isinstance(revs, str):
            revs = [revs]

        self.revs = revs
        self.branch = branch
        self._id = None
        self._date = None

    @property
    def rev(self):
        return self.revs[0]

    @property
    def backedoutby(self):
        """The revision of the commit which backs out this one or None.

        Returns:
            str or None: The commit revision which backs this push out (or None).
        """
        return self._hgmo.get('backedoutby') or None

    @property
    def backedout(self):
        """Whether the push was backed out or not.

        Returns:
            bool: True if this push was backed out.
        """
        return bool(self.backedoutby)

    @property
    def date(self):
        """The push date.

        Returns:
            int: The push date in ms since the epoch.
        """
        if self._date:
            return self._date

        self._date = self._hgmo['pushdate'][0]
        return self._date

    @property
    def id(self):
        """The push id.

        Returns:
            int: The push id.
        """
        if self._id:
            return self._id

        self._id = self._hgmo['pushid']
        return self._id

    def create_push(self, push_id):
        url = HGMO_JSON_PUSHES_URL.format(
            branch=self.branch,
            push_id_start=push_id - 1,
            push_id_end=push_id
        )

        r = requests.get(url)
        r.raise_for_status()
        result = r.json()["pushes"][str(push_id)]

        push = Push(result["changesets"][::-1])
        # avoids the need to query hgmo to find this info
        push._id = push_id
        push._date = result["date"]

        return push

    @memoized_property
    def parent(self):
        """Returns the parent push of this push.

        Returns:
            Push: A `Push` instance representing the parent push.
        """
        return self.create_push(self.id - 1)

    @memoized_property
    def child(self):
        """Returns the child push of this push.

        Returns:
            Push: A `Push` instance representing the child push.
        """
        return self.create_push(self.id + 1)

    @memoized_property
    def tasks(self):
        """All tasks that ran on the push, including retriggers and backfills.

        Returns:
            list: A list of `Task` objects.
        """

        args = Namespace(rev=self.rev, branch=self.branch)
        tasks = defaultdict(dict)

        def add(data):
            for task in data:
                if 'id' not in task:
                    continue
                tasks[task['id']].update(task)

        # Gather information from the treeherder and task tables.
        for table in ('treeherder', 'task', 'unittest'):
            add(run_query('push_tasks_from_{}'.format(table), args)['data'])

        # If we are missing one of these keys, discard the task.
        required_keys = (
            'classification',
            'duration',
            'id',
            'kind',
            'label',
            'result',
        )

        # Normalize and validate.
        normalized_tasks = []
        for task in tasks.values():
            missing = [k for k in required_keys if k not in task]
            taskstr = task.get('label', task['id'])

            if missing:
                logger.trace(f"Skipping task '{taskstr}' because it is missing "
                             f"the following attributes: {', '.join(missing)}")
                continue

            if task['duration'] <= 0:
                logger.trace(f"Skipping task '{taskstr}' because has an invalid duration.")
                continue

            if task.get('tags'):
                task['tags'] = {t['name']: t['value'] for t in task['tags']}

            normalized_tasks.append(task)

        return [Task.create(**task) for task in normalized_tasks]

    @property
    def task_labels(self):
        """The set of task labels that ran on this push.

        Returns:
            set: A set of task labels (str).
        """
        return set([t.label for t in self.tasks])

    @memoized_property
    def target_task_labels(self):
        """The set of all task labels that could possibly run on this push.

        Returns:
            set: A set of task labels.
        """
        return set(self._get_decision_artifact('target-tasks.json'))

    @memoized_property
    def scheduled_task_labels(self):
        """The set of task labels that were originally scheduled to run on this push.

        This excludes backfills and Add New Jobs.

        Returns:
            set: A set of task labels (str).
        """
        tasks = self._get_decision_artifact('task-graph.json').values()
        return {t['label'] for t in tasks}

    @property
    def unscheduled_task_labels(self):
        """The set of task labels from tasks that were not originally scheduled on
        the push (i.e they were scheduled via backfill or Add New Jobs).

        Returns:
            set: A set of task labels (str).
        """
        return self.task_labels - self.scheduled_task_labels

    @memoized_property
    def label_summaries(self):
        """All label summaries combining retriggers.

        Returns:
            dict: A dictionary of the form {<label>: [<LabelSummary>]}.
        """
        labels = defaultdict(list)
        for task in self.tasks:
            labels[task.label].append(task)
        labels = {label: LabelSummary(label, tasks) for label, tasks in labels.items()}
        return labels

    @memoized_property
    def duration(self):
        """The total duration of all tasks that ran on the push.

        Returns:
            int: Runtime in hours.
        """
        return int(sum(t.duration for t in self.tasks) / 3600)

    @memoized_property
    def scheduled_duration(self):
        """The total runtime of tasks excluding retriggers and backfills.

        Returns:
            int: Runtime in hours.
        """
        seen = set()
        duration = 0
        for task in self.tasks:
            if task.label not in self.scheduled_task_labels:
                continue

            if task.label in seen:
                continue

            seen.add(task.label)
            duration += task.duration

        return int(duration / 3600)

    @memoized_property
    def candidate_regressions(self):
        """The set of task labels that are regression candidates for this push.

        A candidate regression is any task label for which at least one
        associated task failed (therefore including intermittents), and which
        is either not classified or fixed by commit.

        Returns:
            set: Set of task labels (str).
        """
        failclass = ('not classified', 'fixed by commit')

        passing_labels = set()
        candidate_regressions = {}

        count = 0
        other = self
        while count < MAX_DEPTH + 1:
            for label, summary in other.label_summaries.items():
                if label in passing_labels:
                    # It passed in one of the pushes between the current and its
                    # children, so it is definitely not a regression in the current.
                    continue

                if summary.status == Status.PASS:
                    passing_labels.add(label)
                    continue

                if all(c not in failclass for c in summary.classifications):
                    passing_labels.add(label)
                    continue

                candidate_regressions[label] = count

            other = other.child
            count += 1

        return candidate_regressions

    @memoized_property
    def regressions(self):
        """All regressions, both likely and definite.

        Each regression is associated with an integer, which is the number of
        parent and children pushes that didn't run the label. A count of 0 means
        the label failed on the current push and passed on the previous push.
        A count of 3 means there were three pushes between the failure and the
        last time the task passed (so any one of them could have caused it).
        A count of MAX_DEPTH means that the maximum number of parents were
        searched without finding the task and we gave up.

        Returns:
            dict: A dict of the form {<label>: <int>}.
        """
        regressions = {}

        for label, child_count in self.candidate_regressions.items():
            count = 0
            other = self.parent
            prior_regression = False

            while count < MAX_DEPTH:
                if label in other.task_labels:
                    if other.label_summaries[label].status != Status.PASS:
                        prior_regression = True
                    break

                other = other.parent
                count += 1

            total_count = count + child_count

            if not prior_regression and total_count <= MAX_DEPTH:
                regressions[label] = total_count

        return regressions

    @property
    def possible_regressions(self):
        """The set of all task labels that may have been regressed by this push.

        A possible regression is a candidate_regression that didn't run on one or
        more parent pushes.

        Returns:
            set: Set of task labels (str).
        """
        return set(label for label, count in self.regressions.items() if count > 0)

    @property
    def likely_regressions(self):
        """The set of all task labels that were likely regressed by this push.

        A likely regression is a candidate_regression that both ran and passed
        on the immediate parent push. It still isn't a sure thing as the task
        could be intermittent.

        Returns:
            set: Set of task labels (str).
        """
        return set(label for label, count in self.regressions.items() if count == 0)

    @memoize
    def get_shadow_scheduler_tasks(self, name):
        """Returns all tasks the given shadow scheduler would have scheduler,
        or None if the given scheduler didn't run.

        Args:
            name (str): The name of the shadow scheduler to query.

        Returns:
            list: All task labels that would have been scheduler or None.
        """
        # First look for an index.
        url = SHADOW_SCHEDULER_ARTIFACT_URL.format(rev=self.rev, name=name)
        r = requests.get(url)

        if r.status_code != 200:
            if name not in self._shadow_scheduler_artifacts:
                return None
            r = requests.get(self._shadow_scheduler_artifacts[name])

        tasks = r.text
        return set(tasks.splitlines())

    @memoize
    def _get_artifact_urls_from_label(self, label):
        """All artifact urls from any task whose label contains ``label``.

        Args:
            label (str): Substring to filter task labels by.

        Returns:
            list: A list of urls.
        """
        return run_query('label_artifacts', Namespace(rev=self.rev, label=label))['data']

    @memoize
    def _get_decision_artifact(self, name):
        """Get an artifact from Decision task of this push.

        Args:
            name (str): Name of the artifact fetch.

        Returns:
            dict: JSON representation of the artifact.
        """
        url = TASKGRAPH_ARTIFACT_URL.format(rev=self.rev, artifact=name)
        r = requests.get(url)
        if r.status_code != 200:
            logger.warning(f"No decision task with artifact {name} on {self.rev}.")
            raise MissingDataError("No decision task on {self.rev}!")
        return r.json()

    @memoized_property
    def _shadow_scheduler_artifacts(self):
        """Get the tasks artifact from the shadow scheduler task called 'name'.

        Returns:
            dict: A mapping of {<shadow scheduler name>: <tasks>}.
        """
        artifacts = {}
        for task in self._get_artifact_urls_from_label('shadow-scheduler'):
            if 'artifacts' not in task:
                continue

            label = task['label']
            found_url = None
            for url in task['artifacts']:
                if url.rsplit('/', 1)[1] == 'optimized_tasks.list':
                    found_url = url
            index = label.find('shadow-scheduler-') + len('shadow-scheduler-')
            artifacts[label[index:]] = found_url

        return artifacts

    @memoized_property
    def _hgmo(self):
        """A JSON dict obtained from hg.mozilla.org.

        Returns:
            dict: Information regarding this push.
        """
        url = HGMO_JSON_URL.format(branch=self.branch, rev=self.rev)
        r = requests.get(url)
        r.raise_for_status()
        return r.json()

    def __repr__(self):
        return f"{super(Push, self).__repr__()} rev='{self.rev}'"


def make_push_objects(**kwargs):
    push_min, push_max = run_query(
        "push_revision_count", Namespace(**kwargs))["data"][0]

    CHUNK_SIZE = 10000
    pushes_groups = [(i, min(i+CHUNK_SIZE-1, push_max))
                     for i in range(push_min, push_max, CHUNK_SIZE)]

    pushes = []
    cur = None

    data = []
    for pushes_group in pushes_groups:
        kwargs["from_push"] = pushes_group[0]
        kwargs["to_push"] = pushes_group[1]

        data += run_query("push_revisions", Namespace(**kwargs))["data"]

    for pushid, date, revs, parents in data:
        topmost = list(set(revs) - set(parents))[0]

        cur = Push([topmost] + [r for r in revs if r != topmost])

        # avoids the need to query hgmo to find this info
        cur._id = pushid
        cur._date = date

        pushes.append(cur)

    for i, cur in enumerate(pushes):
        if i != 0:
            cur._parent = pushes[i - 1]

        if i != len(pushes) - 1:
            cur._child = pushes[i + 1]

    pushes.sort(key=lambda p: p._id)

    return pushes