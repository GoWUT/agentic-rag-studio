"""Composition root for incremental Phase 3 stores and runtimes."""
from server.memory import MemoryStore
from server.data_assets import DataStore
from server.analysis_runtime import AnalysisRuntime
from server.tasks import TaskStore, TaskService
from server.db.repositories import RepositoryFactory


class Phase3Services:
    def __init__(self, manager):
        config = manager.config
        repositories = getattr(manager, 'repositories', None)
        if repositories is None:
            repositories = RepositoryFactory(manager.store.database_path)
        self.memory = repositories.build(MemoryStore,config)
        self.data = repositories.build(DataStore,manager.workspace/'runtime',config)
        self.analysis = AnalysisRuntime(self.data,config)
        self.tasks = repositories.build(TaskStore,config.get('TASK_MAX_STEP_ATTEMPTS',2))
        self.task_service = TaskService(manager,self.tasks)
