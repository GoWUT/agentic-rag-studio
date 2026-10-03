import os


def load_security_config(config, boolean, positive_int):
    config['APP_ENV'] = os.getenv('APP_ENV','development').strip().lower()
    for name,default in {'AUTH_ENABLED':False, 'AUTH_ALLOW_REGISTRATION':True,
        'API_DOCS_ENABLED':True, 'OTEL_ENABLED':False, 'METRICS_ENABLED':True,
        'ALLOW_INSECURE_PRODUCTION_AUTH':False}.items():
        config[name] = boolean(name, default)
    for name,default in {'AUTH_JWT_SECRET':'', 'AUTH_JWT_ISSUER':'agentic-rag-studio',
        'AUTH_JWT_AUDIENCE':'agentic-rag-studio-api', 'CORS_ALLOWED_ORIGINS':'', 'TRUSTED_HOSTS':'',
        'OTEL_SERVICE_NAME':'agentic-rag-api', 'OTEL_EXPORTER_OTLP_ENDPOINT':'', 'LOG_FORMAT':'text'}.items():
        config[name] = os.getenv(name, default).strip()
    for name,default in {'AUTH_ACCESS_TOKEN_MINUTES':20, 'AUTH_REFRESH_TOKEN_DAYS':7,
        'AUTH_PASSWORD_MIN_LENGTH':12, 'AUTH_PASSWORD_MAX_LENGTH':128,
        'AUTH_MAX_FAILED_LOGIN_ATTEMPTS':5, 'AUTH_LOCKOUT_MINUTES':15}.items():
        config[name] = positive_int(name, default)
    if config['AUTH_PASSWORD_MIN_LENGTH'] > config['AUTH_PASSWORD_MAX_LENGTH']:
        raise ValueError('Password length bounds invalid')
    if config['AUTH_ENABLED'] and (len(config['AUTH_JWT_SECRET'].encode()) < 32 or
        not config['AUTH_JWT_ISSUER'] or not config['AUTH_JWT_AUDIENCE']):
        raise ValueError('Authentication requires a strong AUTH_JWT_SECRET and issuer/audience')
    if config['APP_ENV'] == 'production':
        if not config['AUTH_ENABLED'] and not config['ALLOW_INSECURE_PRODUCTION_AUTH']:
            raise ValueError('Production requires AUTH_ENABLED=true')
        if not config['TRUSTED_HOSTS'] or '*' in [v.strip() for v in config['TRUSTED_HOSTS'].split(',')]:
            raise ValueError('Production requires explicit TRUSTED_HOSTS')
        if '*' in [v.strip() for v in config['CORS_ALLOWED_ORIGINS'].split(',')]:
            raise ValueError('Production CORS must use explicit origins')
    if config['LOG_FORMAT'] not in {'text','json'}:
        raise ValueError('LOG_FORMAT must be text or json')
