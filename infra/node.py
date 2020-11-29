from infra.device import Device
from core import util
from collections import OrderedDict

class Node(object):
    def __init__(self, rack_id, node_id, gpu_memory_capacity=0, cpus=0, gpus=0, memory=0, enable_pack=False):
        self.node_id = str(node_id)
        self.cpu_count = cpus
        self.gpu_count = gpus
        self.gpu_memory_capacity = gpu_memory_capacity
        self.mem_size = memory
        self.rack_id = rack_id
        self.network_usage = 0
        self.cpu_used = 0
        self.gpu_used = 0
        self.mem_used = 0
        self.enable_pack = enable_pack
        self.device_cache = OrderedDict()
        for idx in range(0, self.gpu_count):
            self.device_cache[idx] = Device(idx, self.node_id, self.gpu_memory_capacity, self.enable_pack)

        # NOTE: all tasks are deep copied so can be safely deleted upon finished
        # we just wanted to keep track of the running tasks, 
        # and the placed_jobs was for tracking
        self.running_tasks = {}
        self.placed_tasks = {}
        self.placed_jobs = {}
        self.finished_tasks = []

    def get_network_usage(self):
        # assumed the jjob can 
        return self.network_usage

    def check_util(self):
        result = (float(self.cpu_used) / float(self.cpu_count),
                  float(self.gpu_used) / float(self.gpu_count),
                  float(self.mem_used) / float(self.mem_size))

        return result

    def resize_node(self, cpus, gpus, memory):
        self.cpu_count = cpus
        self.gpu_count = gpus
        self.mem_size = memory

    def cpu_free(self):
        result = (self.cpu_count - self.cpu_used)
        return result

    def gpu_free(self):
        result = (self.gpu_count - self.gpu_used)
        return result

    def mem_free(self):
        result = (self.mem_size - self.mem_used)
        return result

    def is_free(self):
        return self.gpu_free() > 0 or self.cpu_free() > 0 or self.mem_free() > 0

    def reset_resource(self, num=0, gpu=1, cpu=4, mem=6):
        assert len(self.running_tasks) >= num
        self.gpu_used = num * gpu
        self.cpu_used = num * cpu
        self.mem_used = num * mem

    def release_allocated_resources(self, task):
        """NOTE: release"""
        # clear tasks
        self.cpu_used -= task.cpu
        self.gpu_used -= task.gpu
        assert self.gpu_used >= 0
        self.mem_used -= task.mem
        if task.task_id in self.gpu_mem_utilizations:
            self.gpu_mem_utilizations.pop(task.task_id)
        self.finished_tasks.append(task.task_id)

    def can_fit_num_task(self, tasks, pack=False):
        """
        NOTE: CRITICAL STUFF
        return integer, -1 if can fit all tasks.
        """
        if pack:
            return self._can_fit_num_with_pack(tasks)
        
        first_task = next(iter(tasks.values()))
        cpu_per_task = first_task.cpu
        mem_per_task = first_task.mem

        gpus_task_offset = (self.gpu_free() // first_task.gpu) - len(tasks)
        cpus_task_offset = (self.cpu_free() // cpu_per_task) - len(tasks)
        mems_task_offset = (self.mem_free() // mem_per_task) - len(tasks)
        num_tasks_gpus_can_fit = len(tasks) if gpus_task_offset >= 0 else len(tasks) + gpus_task_offset
        num_tasks_cpus_can_fit = len(tasks) if cpus_task_offset >= 0 else len(tasks) + cpus_task_offset
        num_tasks_mems_can_fit = len(tasks) if mems_task_offset >= 0 else len(tasks) + mems_task_offset
        return min(min(num_tasks_cpus_can_fit, num_tasks_mems_can_fit), num_tasks_gpus_can_fit)

    def _can_fit_num_with_pack(self, tasks):
        count = 0
        for _, t in tasks.items():
            if self.gpu_free() < t.gpu or self.cpu_free() < t.cpu or self.mem_free() < t.mem:
                continue

            for _, d in self.device_cache.items():
                # safety margin 500 Mb ? 
                if d.memory - (d.get_current_memory() + t.gpu_memory_max) > 500:
                    count += 1
        return count

    def can_fit(self, task):
        cpu_offset = self.cpu_free() - task.cpu
        mem_offset = self.mem_free() - task.mem
        gpu_offset = self.gpu_free() - task.gpu
        result = (cpu_offset >= 0) and (mem_offset >= 0) and (gpu_offset >= 0)
        return result

    def execute_job(self, job_id, delta_time):
        # check placed tasks in current node that is correspond to same job_id
        started_task_count = 0
        job_to_execute = self.placed_jobs.pop(job_id, None)
        if job_to_execute is None:
            raise ValueError()

        # logging.info("Job: %s --- tasks running on: %s" % (job_id, job_to_execute.tasks_running_on))
        running_nodes = set(job_to_execute.tasks_running_on.values())
        #logging.info("running nodes: %s" % (running_nodes))
        if self.node_id not in running_nodes:
            raise ValueError()

        for k, v in iter(job_to_execute.tasks_running_on.items()):
            #logging.info("%s - %s" % (k,v))
            if v == self.node_id:
                jt = self.placed_tasks.pop(k)
                jt.execute(delta_time)
                self.running_tasks[k] = jt
                #logging.info("executing task: %s" % k)
        result, started_task_count = job_to_execute.try_execute(delta_time)
        if result:
            return job_to_execute, started_task_count
        return None, started_task_count

    def try_reserve_and_placed_task(self, task):
        result = self.can_fit(task)
        if not result:
            return result
        assert self.gpu_used + task.gpu <= self.gpu_count
        self.cpu_used += task.cpu
        self.mem_used += task.mem
        self.gpu_used += task.gpu
        self.placed_tasks[task.task_id] = task
        return result

    def try_reserve_and_placed_job(self, job, count_task_in_current_node=False):
        """
        NOTE: 
            param:
            count_task_in_current_node: if count_task_in_current_node is True, 
            will count whether all the task in the job is in current node placed_tasks
        """
        found = False
        for t in iter(job.tasks.keys()):
            if t not in self.placed_tasks:
                if not count_task_in_current_node:
                    continue
                else:
                    return False
            else:
                # util.print_fn("Found at least 1 task %s, in node %s" % (t, self.node_id))
                found = True
                break

        self.placed_jobs[job.job_id] = job
        return found

    def try_alloc_job(self, job, is_single=False):
        """
        NOTE: right now this assume all tasks can fit then we placed the tasks and corresponding job.
        """
        result = False
        worker_tasks = self.can_fit_num_task(job.tasks)

        if worker_tasks >= job.task_count:
            copy_j = job.tasks.copy()
            placed = 0
            for t in iter(copy_j.values()):
                result = self.try_reserve_and_placed_task(t)
                if result:
                    job.tasks_running_on[t.task_id] = self.node_id
                    placed += 1
            if placed > 0:
                result = self.try_reserve_and_placed_job(job, is_single)
                if not result:
                    # Not executed yet
                    for jt in job.tasks.items():
                        job.tasks_running_on.pop(jt.task_id, None)
                        self.placed_tasks.pop(jt.task_id, None)
                        self.release_allocated_resources(jt)
                    self.placed_jobs.pop(job.job_id)
                    util.print_fn("RELEASED: Job does not fit on node", util.LOG_LEVEL_WARNING)
                    return result
                util.print_fn(
                    "placed SINGLE NODE job %s, num tasks %d on node %s" % (job.job_id, len(job.tasks), self.node_id))
        else:
            util.print_fn("Job does not fit on node", util.LOG_LEVEL_WARNING)
        return result
