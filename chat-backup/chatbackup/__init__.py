"""chat-backup: keeps a local, browsable copy of every ChatGPT chat.

Modules, in the order data flows through them:

- config    reads the few settings from environment variables
- browser   drives a logged-in Chromium and calls ChatGPT's internal API from inside the page
- chatgpt   knows the API's endpoints, JSON shapes and error responses
- db        SQLite bookkeeping: which chats exist, which need fetching, which were deleted
- render    turns a raw conversation into a Markdown transcript, canvas files and the index
- archive   writes everything into the archive folder
- notify    phone alerts through ntfy
- main      the polling loop that ties it all together
"""
