# core — cross-cutting JURIS infrastructure
#
# Responsibilities:
#   config.py       — centralised Settings object (env / .env file)
#   identity.py     — who is making this request (JurisUser, RequestIdentity)
#   security.py     — authentication verification boundary (TokenVerifier)
#   permissions.py  — what they are allowed to do (PermissionEngine interface)
