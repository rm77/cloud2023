from locust import HttpUser, task, between

class User(HttpUser):
    wait_time = between(0.01, 0.05)

    @task
    def cpu_request(self):
        self.client.get('/cpu')
