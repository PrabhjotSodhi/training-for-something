import os

# tfs start checks GitHub for updates in a thread. No test may reach GitHub.
os.environ["TFS_NO_UPDATE_CHECK"] = "1"
