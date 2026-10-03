"""Argon2 accounts, fixed-algorithm JWT and transactional opaque refresh rotation."""
from datetime import datetime,timedelta,timezone
import hashlib
import re
import secrets
import jwt
from fastapi import HTTPException
from pwdlib import PasswordHash
from server.persistence import identity, now
from server.auth.context import AuthenticatedPrincipal


def utc(value):
    return datetime.fromisoformat(value) if isinstance(value,str) else value


class PasswordService:
    def __init__(self,config):
        self.config=config
        self.hasher=PasswordHash.recommended()
        self.dummy=self.hasher.hash(secrets.token_urlsafe(32))

    def hash_password(self,password):
        if not self.config.get('AUTH_PASSWORD_MIN_LENGTH',12) <= len(password) <= self.config.get('AUTH_PASSWORD_MAX_LENGTH',128):
            raise HTTPException(422,'Password length outside policy')
        return self.hasher.hash(password)

    def verify_password(self,password,hashed):
        if not isinstance(password,str) or len(password)>self.config.get('AUTH_PASSWORD_MAX_LENGTH',128):
            return False
        try:
            return self.hasher.verify(password,hashed)
        except Exception:
            return False


class AuthService:
    def __init__(self,store,config):
        self.store,self.config=store,config
        if len(config.get('AUTH_JWT_SECRET','').encode())<32 or not config.get('AUTH_JWT_ISSUER') or not config.get('AUTH_JWT_AUDIENCE'):
            raise ValueError('Authentication requires a strong signing secret and issuer/audience')
        self.passwords=PasswordService(config)

    @staticmethod
    def public(user):
        return {k:user[k] for k in ('id','email','display_name','status')}

    def register(self,email,password,display_name=''):
        normalized=email.strip().lower()
        if len(normalized)>254 or not re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+',normalized):
            raise HTTPException(422,'Invalid email')
        password_hash=self.passwords.hash_password(password)
        user_id=identity(); stamp=now()
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            if self.store.user(db,email=normalized):
                raise HTTPException(409,'Account could not be created')
            db.execute('INSERT INTO users (id,email,normalized_email,display_name,password_hash,status,failed_login_count,locked_until,created_at,updated_at,last_login_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                (user_id,email.strip(),normalized,display_name.strip()[:200],password_hash,'ACTIVE',0,None,stamp,stamp,None))
            self.store.audit(db,'USER_REGISTERED',actor=user_id,resource_id=user_id)
            return self.public(self.store.user(db,user_id=user_id))

    def access(self,user_id):
        stamp=datetime.now(timezone.utc)
        return jwt.encode({'sub':user_id,'jti':identity(),'type':'access','iat':stamp,
            'exp':stamp+timedelta(minutes=self.config.get('AUTH_ACCESS_TOKEN_MINUTES',20)),
            'iss':self.config['AUTH_JWT_ISSUER'],'aud':self.config['AUTH_JWT_AUDIENCE']},
            self.config['AUTH_JWT_SECRET'],algorithm='HS256')

    def current_principal(self,token):
        try:
            claims=jwt.decode(token,self.config['AUTH_JWT_SECRET'],algorithms=['HS256'],
                issuer=self.config['AUTH_JWT_ISSUER'],audience=self.config['AUTH_JWT_AUDIENCE'],
                options={'require':['sub','jti','type','iat','exp','iss','aud']})
            if claims['type']!='access' or not isinstance(claims['sub'],str) or not claims['jti']:
                raise ValueError()
        except (jwt.InvalidTokenError,ValueError,TypeError):
            raise HTTPException(401,'Invalid access token',headers={'WWW-Authenticate':'Bearer'}) from None
        return self.principal_for_user(claims['sub'])

    def principal_for_user(self,user_id):
        user=self.store.principal_user(user_id)
        if not user or user['status']!='ACTIVE':
            raise HTTPException(401,'Account unavailable')
        return AuthenticatedPrincipal(user['id'],user['email'],user['status'])

    @staticmethod
    def token_hash(token):
        return hashlib.sha256(token.encode()).hexdigest()

    def new_refresh(self,db,user_id,family=None,parent=None):
        token=secrets.token_urlsafe(48); token_id=identity()
        db.execute('INSERT INTO refresh_tokens (id,user_id,token_hash,family_id,parent_id,expires_at,created_at,used_at,revoked_at,replaced_by,user_agent_hash) VALUES (?,?,?,?,?,?,?,?,?,?,?)',
            (token_id,user_id,self.token_hash(token),family or identity(),parent,
             (datetime.now(timezone.utc)+timedelta(days=self.config.get('AUTH_REFRESH_TOKEN_DAYS',7))).isoformat(),
             now(),None,None,None,None))
        return token,token_id

    def login(self,email,password):
        normalized=email.strip().lower()
        with self.store.connect() as db:
            user=self.store.user(db,email=normalized)
        verified=self.passwords.verify_password(password,user['password_hash'] if user else self.passwords.dummy)
        tokens=None
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            current=self.store.user(db,email=normalized)
            stamp=datetime.now(timezone.utc)
            locked=bool(current and current['locked_until'] and utc(current['locked_until'])>stamp)
            valid=bool(current and user and current['password_hash']==user['password_hash'] and
                       current['status']=='ACTIVE' and verified and not locked)
            if valid:
                db.execute('UPDATE users SET failed_login_count=0,locked_until=NULL,last_login_at=?,updated_at=? WHERE id=?',
                           (now(),now(),current['id']))
                refresh,_=self.new_refresh(db,current['id'])
                self.store.audit(db,'LOGIN_SUCCEEDED',actor=current['id'],resource_id=current['id'])
                tokens={'access_token':self.access(current['id']),'refresh_token':refresh,'token_type':'bearer'}
            else:
                if current and current['status']=='ACTIVE' and not locked:
                    count=(0 if current['locked_until'] else current['failed_login_count'])+1
                    until=(stamp+timedelta(minutes=self.config.get('AUTH_LOCKOUT_MINUTES',15))).isoformat() if count>=self.config.get('AUTH_MAX_FAILED_LOGIN_ATTEMPTS',5) else None
                    db.execute('UPDATE users SET failed_login_count=?,locked_until=?,updated_at=? WHERE id=?',
                               (count,until,now(),current['id']))
                    if until:self.store.audit(db,'USER_LOCKED',actor=current['id'],resource_id=current['id'])
                self.store.audit(db,'LOGIN_FAILED',actor=current['id'] if current else None,result='DENIED')
        if not tokens:raise HTTPException(401,'Invalid credentials')
        return tokens

    def refresh(self,token):
        tokens=None
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row=db.execute('SELECT * FROM refresh_tokens WHERE token_hash=?',(self.token_hash(token),)).fetchone()
            if row:
                user=self.store.user(db,user_id=row['user_id'])
                if row['used_at']:
                    db.execute('UPDATE refresh_tokens SET revoked_at=? WHERE family_id=?',(now(),row['family_id']))
                    self.store.audit(db,'TOKEN_REPLAY_DETECTED',actor=row['user_id'],result='DENIED',metadata={'family_id':row['family_id']})
                elif not row['revoked_at'] and utc(row['expires_at'])>datetime.now(timezone.utc) and user and user['status']=='ACTIVE':
                    next_token,next_id=self.new_refresh(db,row['user_id'],row['family_id'],row['id'])
                    db.execute('UPDATE refresh_tokens SET used_at=?,revoked_at=?,replaced_by=? WHERE id=?',(now(),now(),next_id,row['id']))
                    self.store.audit(db,'TOKEN_REFRESHED',actor=row['user_id'],metadata={'family_id':row['family_id']})
                    tokens={'access_token':self.access(row['user_id']),'refresh_token':next_token,'token_type':'bearer'}
        if not tokens:raise HTTPException(401,'Invalid refresh token')
        return tokens

    def logout(self,token):
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row=db.execute('SELECT user_id,id FROM refresh_tokens WHERE token_hash=?',(self.token_hash(token),)).fetchone()
            if row:
                db.execute('UPDATE refresh_tokens SET revoked_at=? WHERE id=?',(now(),row['id']))
                self.store.audit(db,'LOGOUT',actor=row['user_id'])

    def logout_all(self,user_id):
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('UPDATE refresh_tokens SET revoked_at=? WHERE user_id=?',(now(),user_id))
            self.store.audit(db,'LOGOUT',actor=user_id,metadata={'action':'all'})

    def change_password(self,user_id,old_password,new_password):
        user=self.store.principal_user(user_id)
        if not user or not self.passwords.verify_password(old_password,user['password_hash']):
            raise HTTPException(401,'Invalid credentials')
        hashed=self.passwords.hash_password(new_password)
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            current=self.store.user(db,user_id=user_id)
            if current['password_hash']!=user['password_hash']:raise HTTPException(409,'Account changed')
            db.execute('UPDATE users SET password_hash=?,updated_at=? WHERE id=?',(hashed,now(),user_id))
            db.execute('UPDATE refresh_tokens SET revoked_at=? WHERE user_id=?',(now(),user_id))
            self.store.audit(db,'PASSWORD_CHANGED',actor=user_id)
