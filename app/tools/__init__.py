"""Read-only repository inspection tools for the agent.

Core functions: ``app.tools.repo`` (list_files / read_file / search_code).
LangChain wrappers: ``app.tools.registry`` (bound to one repository path).
Read-only Git tools: ``app.tools.git_tools`` (bound to one workspace).
Controlled test tools: ``app.tools.build_tools`` (fixed Maven/Gradle test).
"""
