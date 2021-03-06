#!/usr/bin/env python

# Brendio's Passphrase to private key converter	
# convertphrase.py 0.1
# based on http://github.com/gavinandresen/bitcointools and pywallet.py
#
# Usage: convertphrase.py [options]
#
# Options:
#   --version              show program's version number and exit
#   -h, --help             show this help message and exit
#   --phrase="KEYSTR"  convert the passphrase "KEYSTR" to a private key hash

from bsddb.db import *
import os, sys, time
import json
import logging
import struct
import StringIO
import traceback
import socket
import types
import string
import exceptions
import hashlib
import random

max_version = 32400
addrtype = 0
json_db = {}
private_keys = []

def determine_db_dir():
	import os
	import os.path
	import platform
	if platform.system() == "Darwin":
		return os.path.expanduser("~/Library/Application Support/Bitcoin/")
	elif platform.system() == "Windows":
		return os.path.join(os.environ['APPDATA'], "Bitcoin")
	return os.path.expanduser("~/.bitcoin")

# secp256k1

_p = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEFFFFFC2FL
_r = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141L
_b = 0x0000000000000000000000000000000000000000000000000000000000000007L
_a = 0x0000000000000000000000000000000000000000000000000000000000000000L
_Gx = 0x79BE667EF9DCBBAC55A06295CE870B07029BFCDB2DCE28D959F2815B16F81798L
_Gy = 0x483ada7726a3c4655da4fbfc0e1108a8fd17b448a68554199c47d08ffb10d4b8L

class CurveFp( object ):
	def __init__( self, p, a, b ):
		self.__p = p
		self.__a = a
		self.__b = b

	def p( self ):
		return self.__p

	def a( self ):
		return self.__a

	def b( self ):
		return self.__b

	def contains_point( self, x, y ):
		return ( y * y - ( x * x * x + self.__a * x + self.__b ) ) % self.__p == 0

class Point( object ):
	def __init__( self, curve, x, y, order = None ):
		self.__curve = curve
		self.__x = x
		self.__y = y
		self.__order = order
		if self.__curve: assert self.__curve.contains_point( x, y )
		if order: assert self * order == INFINITY
 
	def __add__( self, other ):
		if other == INFINITY: return self
		if self == INFINITY: return other
		assert self.__curve == other.__curve
		if self.__x == other.__x:
			if ( self.__y + other.__y ) % self.__curve.p() == 0:
				return INFINITY
			else:
				return self.double()

		p = self.__curve.p()
		l = ( ( other.__y - self.__y ) * \
					inverse_mod( other.__x - self.__x, p ) ) % p
		x3 = ( l * l - self.__x - other.__x ) % p
		y3 = ( l * ( self.__x - x3 ) - self.__y ) % p
		return Point( self.__curve, x3, y3 )

	def __mul__( self, other ):
		def leftmost_bit( x ):
			assert x > 0
			result = 1L
			while result <= x: result = 2 * result
			return result / 2

		e = other
		if self.__order: e = e % self.__order
		if e == 0: return INFINITY
		if self == INFINITY: return INFINITY
		assert e > 0
		e3 = 3 * e
		negative_self = Point( self.__curve, self.__x, -self.__y, self.__order )
		i = leftmost_bit( e3 ) / 2
		result = self
		while i > 1:
			result = result.double()
			if ( e3 & i ) != 0 and ( e & i ) == 0: result = result + self
			if ( e3 & i ) == 0 and ( e & i ) != 0: result = result + negative_self
			i = i / 2
		return result

	def __rmul__( self, other ):
		return self * other

	def __str__( self ):
		if self == INFINITY: return "infinity"
		return "(%d,%d)" % ( self.__x, self.__y )

	def double( self ):
		if self == INFINITY:
			return INFINITY

		p = self.__curve.p()
		a = self.__curve.a()
		l = ( ( 3 * self.__x * self.__x + a ) * \
					inverse_mod( 2 * self.__y, p ) ) % p
		x3 = ( l * l - 2 * self.__x ) % p
		y3 = ( l * ( self.__x - x3 ) - self.__y ) % p
		return Point( self.__curve, x3, y3 )

	def x( self ):
		return self.__x

	def y( self ):
		return self.__y

	def curve( self ):
		return self.__curve
	
	def order( self ):
		return self.__order
		
INFINITY = Point( None, None, None )

def inverse_mod( a, m ):
	if a < 0 or m <= a: a = a % m
	c, d = a, m
	uc, vc, ud, vd = 1, 0, 0, 1
	while c != 0:
		q, c, d = divmod( d, c ) + ( c, )
		uc, vc, ud, vd = ud - q*uc, vd - q*vc, uc, vc
	assert d == 1
	if ud > 0: return ud
	else: return ud + m

class Signature( object ):
	def __init__( self, r, s ):
		self.r = r
		self.s = s
		
class Public_key( object ):
	def __init__( self, generator, point ):
		self.curve = generator.curve()
		self.generator = generator
		self.point = point
		n = generator.order()
		if not n:
			raise RuntimeError, "Generator point must have order."
		if not n * point == INFINITY:
			raise RuntimeError, "Generator point order is bad."
		if point.x() < 0 or n <= point.x() or point.y() < 0 or n <= point.y():
			raise RuntimeError, "Generator point has x or y out of range."

	def verifies( self, hash, signature ):
		G = self.generator
		n = G.order()
		r = signature.r
		s = signature.s
		if r < 1 or r > n-1: return False
		if s < 1 or s > n-1: return False
		c = inverse_mod( s, n )
		u1 = ( hash * c ) % n
		u2 = ( r * c ) % n
		xy = u1 * G + u2 * self.point
		v = xy.x() % n
		return v == r

class Private_key( object ):
	def __init__( self, public_key, secret_multiplier ):
		self.public_key = public_key
		self.secret_multiplier = secret_multiplier

	def der( self ):
		hex_der_key = '06052b8104000a30740201010420' + \
			'%064x' % self.secret_multiplier + \
			'a00706052b8104000aa14403420004' + \
			'%064x' % self.public_key.point.x() + \
			'%064x' % self.public_key.point.y()

	def sign( self, hash, random_k ):
		G = self.public_key.generator
		n = G.order()
		k = random_k % n
		p1 = k * G
		r = p1.x()
		if r == 0: raise RuntimeError, "amazingly unlucky random number r"
		s = ( inverse_mod( k, n ) * \
					( hash + ( self.secret_multiplier * r ) % n ) ) % n
		if s == 0: raise RuntimeError, "amazingly unlucky random number s"
		return Signature( r, s )

class EC_KEY(object):
	def __init__( self, secret ):
		curve = CurveFp( _p, _a, _b )
		generator = Point( curve, _Gx, _Gy, _r )
		self.pubkey = Public_key( generator, generator * secret )
		self.privkey = Private_key( self.pubkey, secret )
		self.secret = secret

def i2d_ECPrivateKey(pkey):
	hex_i2d_key = '308201130201010420' + \
		'%064x' % pkey.secret + \
		'a081a53081a2020101302c06072a8648ce3d0101022100' + \
		'%064x' % _p + \
		'3006040100040107044104' + \
		'%064x' % _Gx + \
		'%064x' % _Gy + \
		'022100' + \
		'%064x' % _r + \
		'020101a14403420004' + \
		'%064x' % pkey.pubkey.point.x() + \
		'%064x' % pkey.pubkey.point.y()
	return hex_i2d_key.decode('hex')

def i2o_ECPublicKey(pkey):
	hex_i2o_key = '04' + \
		'%064x' % pkey.pubkey.point.x() + \
		'%064x' % pkey.pubkey.point.y()
	return hex_i2o_key.decode('hex')

# hashes

def hash_160(public_key):
 	md = hashlib.new('ripemd160')
	md.update(hashlib.sha256(public_key).digest())
	return md.digest()

def public_key_to_bc_address(public_key):
	h160 = hash_160(public_key)
	return hash_160_to_bc_address(h160)

def hash_160_to_bc_address(h160):
	vh160 = chr(addrtype) + h160
	h = Hash(vh160)
	addr = vh160 + h[0:4]
	return b58encode(addr)

def bc_address_to_hash_160(addr):
	bytes = b58decode(addr, 25)
	return bytes[1:21]

def long_hex(bytes):
	return bytes.encode('hex_codec')

def short_hex(bytes):
	t = bytes.encode('hex_codec')
	if len(t) < 32:
		return t
	return t[0:32]+"..."+t[-32:]

__b58chars = '123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz'
__b58base = len(__b58chars)

def b58encode(v):
	""" encode v, which is a string of bytes, to base58.		
	"""

	long_value = 0L
	for (i, c) in enumerate(v[::-1]):
		long_value += (256**i) * ord(c)

	result = ''
	while long_value >= __b58base:
		div, mod = divmod(long_value, __b58base)
		result = __b58chars[mod] + result
		long_value = div
	result = __b58chars[long_value] + result

	# Bitcoin does a little leading-zero-compression:
	# leading 0-bytes in the input become leading-1s
	nPad = 0
	for c in v:
		if c == '\0': nPad += 1
		else: break

	return (__b58chars[0]*nPad) + result

def b58decode(v, length):
	""" decode v into a string of len bytes
	"""
	long_value = 0L
	for (i, c) in enumerate(v[::-1]):
		long_value += __b58chars.find(c) * (__b58base**i)

	result = ''
	while long_value >= 256:
		div, mod = divmod(long_value, 256)
		result = chr(mod) + result
		long_value = div
	result = chr(long_value) + result

	nPad = 0
	for c in v:
		if c == __b58chars[0]: nPad += 1
		else: break

	result = chr(0)*nPad + result
	if length is not None and len(result) != length:
		return None

	return result

def long_hex(bytes):
	return bytes.encode('hex_codec')

def Hash(data):
	return hashlib.sha256(hashlib.sha256(data).digest()).digest()

def EncodeBase58Check(vchIn):
	hash = Hash(vchIn)
	return b58encode(vchIn + hash[0:4])

def DecodeBase58Check(psz):
	vchRet = b58decode(psz, None)
	key = vchRet[0:-4]
	csum = vchRet[-4:]
	hash = Hash(key)
	cs32 = hash[0:4]
	if cs32 != csum:
		return None
	else:
		return key

def str_to_long(b):
	res = 0
	pos = 1
	for a in reversed(b):
		res += ord(a) * pos
		pos *= 256
	return res

def PrivKeyToSecret(privkey):
	return privkey[9:9+32]

def Add0x80(secret):
	vchIn = chr(addrtype+128) + secret
	return vchIn

def SecretToASecret(secret):
	vchIn = chr(addrtype+128) + secret
	return EncodeBase58Check(vchIn)

def ASecretToSecret(key):
	vch = DecodeBase58Check(key)
	if vch and vch[0] == chr(addrtype+128):
		return vch[1:]
	else:
		return False

def regenerate_key(sec):
	b = ASecretToSecret(sec)
	if not b:
		return False
	secret = str_to_long(b)	
	return EC_KEY(secret)

def GetPubKey(pkey):
	return i2o_ECPublicKey(pkey)

def GetPrivKey(pkey):
	return i2d_ECPrivateKey(pkey)

def GetSecret(pkey):
	return ('%064x' % pkey.secret).decode('hex')

# parser

def create_env(db_dir):
	db_env = DBEnv(0)
	r = db_env.open(db_dir, (DB_CREATE|DB_INIT_LOCK|DB_INIT_LOG|DB_INIT_MPOOL|DB_INIT_TXN|DB_THREAD|DB_RECOVER))
	return db_env

def parse_CAddress(vds):
	d = {'ip':'0.0.0.0','port':0,'nTime': 0}
	try:
		d['nVersion'] = vds.read_int32()
		d['nTime'] = vds.read_uint32()
		d['nServices'] = vds.read_uint64()
		d['pchReserved'] = vds.read_bytes(12)
		d['ip'] = socket.inet_ntoa(vds.read_bytes(4))
		d['port'] = vds.read_uint16()
	except:
		pass
	return d

def deserialize_CAddress(d):
	return d['ip']+":"+str(d['port'])

def parse_BlockLocator(vds):
	d = { 'hashes' : [] }
	nHashes = vds.read_compact_size()
	for i in xrange(nHashes):
		d['hashes'].append(vds.read_bytes(32))
		return d

def deserialize_BlockLocator(d):
  result = "Block Locator top: "+d['hashes'][0][::-1].encode('hex_codec')
  return result

def parse_setting(setting, vds):
	if setting[0] == "f":	# flag (boolean) settings
		return str(vds.read_boolean())
	elif setting[0:4] == "addr": # CAddress
		d = parse_CAddress(vds)
		return deserialize_CAddress(d)
	elif setting == "nTransactionFee":
		return vds.read_int64()
	elif setting == "nLimitProcessors":
		return vds.read_int32()
	return 'unknown setting'

class SerializationError(Exception):
	""" Thrown when there's a problem deserializing or serializing """

class BCDataStream(object):
	def __init__(self):
		self.input = None
		self.read_cursor = 0

	def clear(self):
		self.input = None
		self.read_cursor = 0

	def write(self, bytes):	# Initialize with string of bytes
		if self.input is None:
			self.input = bytes
		else:
			self.input += bytes

	def map_file(self, file, start):	# Initialize with bytes from file
		self.input = mmap.mmap(file.fileno(), 0, access=mmap.ACCESS_READ)
		self.read_cursor = start
	def seek_file(self, position):
		self.read_cursor = position
	def close_file(self):
		self.input.close()

	def read_string(self):
		# Strings are encoded depending on length:
		# 0 to 252 :	1-byte-length followed by bytes (if any)
		# 253 to 65,535 : byte'253' 2-byte-length followed by bytes
		# 65,536 to 4,294,967,295 : byte '254' 4-byte-length followed by bytes
		# ... and the Bitcoin client is coded to understand:
		# greater than 4,294,967,295 : byte '255' 8-byte-length followed by bytes of string
		# ... but I don't think it actually handles any strings that big.
		if self.input is None:
			raise SerializationError("call write(bytes) before trying to deserialize")

		try:
			length = self.read_compact_size()
		except IndexError:
			raise SerializationError("attempt to read past end of buffer")

		return self.read_bytes(length)

	def write_string(self, string):
		# Length-encoded as with read-string
		self.write_compact_size(len(string))
		self.write(string)

	def read_bytes(self, length):
		try:
			result = self.input[self.read_cursor:self.read_cursor+length]
			self.read_cursor += length
			return result
		except IndexError:
			raise SerializationError("attempt to read past end of buffer")

		return ''

	def read_boolean(self): return self.read_bytes(1)[0] != chr(0)
	def read_int16(self): return self._read_num('<h')
	def read_uint16(self): return self._read_num('<H')
	def read_int32(self): return self._read_num('<i')
	def read_uint32(self): return self._read_num('<I')
	def read_int64(self): return self._read_num('<q')
	def read_uint64(self): return self._read_num('<Q')

	def write_boolean(self, val): return self.write(chr(1) if val else chr(0))
	def write_int16(self, val): return self._write_num('<h', val)
	def write_uint16(self, val): return self._write_num('<H', val)
	def write_int32(self, val): return self._write_num('<i', val)
	def write_uint32(self, val): return self._write_num('<I', val)
	def write_int64(self, val): return self._write_num('<q', val)
	def write_uint64(self, val): return self._write_num('<Q', val)

	def read_compact_size(self):
		size = ord(self.input[self.read_cursor])
		self.read_cursor += 1
		if size == 253:
			size = self._read_num('<H')
		elif size == 254:
			size = self._read_num('<I')
		elif size == 255:
			size = self._read_num('<Q')
		return size

	def write_compact_size(self, size):
		if size < 0:
			raise SerializationError("attempt to write size < 0")
		elif size < 253:
			 self.write(chr(size))
		elif size < 2**16:
			self.write('\xfd')
			self._write_num('<H', size)
		elif size < 2**32:
			self.write('\xfe')
			self._write_num('<I', size)
		elif size < 2**64:
			self.write('\xff')
			self._write_num('<Q', size)

	def _read_num(self, format):
		(i,) = struct.unpack_from(format, self.input, self.read_cursor)
		self.read_cursor += struct.calcsize(format)
		return i

	def _write_num(self, format, num):
		s = struct.pack(format, num)
		self.write(s)

def open_wallet(db_env, writable=False):
	db = DB(db_env)
	flags = DB_THREAD | (DB_CREATE if writable else DB_RDONLY)
	try:
		r = db.open("wallet.dat", "main", DB_BTREE, flags)
	except DBError:
		r = True

	if r is not None:
		logging.error("Couldn't open wallet.dat/main. Try quitting Bitcoin and running this again.")
		sys.exit(1)
	
	return db

def parse_wallet(db, item_callback):
	kds = BCDataStream()
	vds = BCDataStream()

	for (key, value) in db.items():
		d = { }

		kds.clear(); kds.write(key)
		vds.clear(); vds.write(value)

		type = kds.read_string()

		d["__key__"] = key
		d["__value__"] = value
		d["__type__"] = type

		try:
			if type == "tx":
				d["tx_id"] = kds.read_bytes(32)
			elif type == "name":
				d['hash'] = kds.read_string()
				d['name'] = vds.read_string()
			elif type == "version":
				d['version'] = vds.read_uint32()
			elif type == "setting":
				d['setting'] = kds.read_string()
				d['value'] = parse_setting(d['setting'], vds)
			elif type == "key":
				d['public_key'] = kds.read_bytes(kds.read_compact_size())
				d['private_key'] = vds.read_bytes(vds.read_compact_size())
			elif type == "wkey":
				d['public_key'] = kds.read_bytes(kds.read_compact_size())
				d['private_key'] = vds.read_bytes(vds.read_compact_size())
				d['created'] = vds.read_int64()
				d['expires'] = vds.read_int64()
				d['comment'] = vds.read_string()
			elif type == "defaultkey":
				d['key'] = vds.read_bytes(vds.read_compact_size())
			elif type == "pool":
				d['n'] = kds.read_int64()
				d['nVersion'] = vds.read_int32()
				d['nTime'] = vds.read_int64()
				d['public_key'] = vds.read_bytes(vds.read_compact_size())
			elif type == "acc":
				d['account'] = kds.read_string()
				d['nVersion'] = vds.read_int32()
				d['public_key'] = vds.read_bytes(vds.read_compact_size())
			elif type == "acentry":
				d['account'] = kds.read_string()
				d['n'] = kds.read_uint64()
				d['nVersion'] = vds.read_int32()
				d['nCreditDebit'] = vds.read_int64()
				d['nTime'] = vds.read_int64()
				d['otherAccount'] = vds.read_string()
				d['comment'] = vds.read_string()
			elif type == "bestblock":
				d['nVersion'] = vds.read_int32()
				d.update(parse_BlockLocator(vds))
			
			item_callback(type, d)

		except Exception, e:
			traceback.print_exc()
			print("ERROR parsing wallet.dat, type %s" % type)
			print("key data in hex: %s"%key.encode('hex_codec'))
			print("value data in hex: %s"%value.encode('hex_codec'))
			sys.exit(1)
	
def update_wallet(db, type, data):
	"""Write a single item to the wallet.
	db must be open with writable=True.
	type and data are the type code and data dictionary as parse_wallet would
	give to item_callback.
	data's __key__, __value__ and __type__ are ignored; only the primary data
	fields are used.
	"""
	d = data
	kds = BCDataStream()
	vds = BCDataStream()

	# Write the type code to the key
	kds.write_string(type)
	vds.write("")						 # Ensure there is something

	try:
		if type == "tx":
			raise NotImplementedError("Writing items of type 'tx'")
			kds.write(d['tx_id'])
		elif type == "name":
			kds.write_string(d['hash'])
			vds.write_string(d['name'])
		elif type == "version":
			vds.write_uint32(d['version'])
		elif type == "setting":
			raise NotImplementedError("Writing items of type 'setting'")
			kds.write_string(d['setting'])
			#d['value'] = parse_setting(d['setting'], vds)
		elif type == "key":
			kds.write_string(d['public_key'])
			vds.write_string(d['private_key'])
		elif type == "wkey":
			kds.write_string(d['public_key'])
			vds.write_string(d['private_key'])
			vds.write_int64(d['created'])
			vds.write_int64(d['expires'])
			vds.write_string(d['comment'])
		elif type == "defaultkey":
			vds.write_string(d['key'])
		elif type == "pool":
			kds.write_int64(d['n'])
			vds.write_int32(d['nVersion'])
			vds.write_int64(d['nTime'])
			vds.write_string(d['public_key'])
		elif type == "acc":
			kds.write_string(d['account'])
			vds.write_int32(d['nVersion'])
			vds.write_string(d['public_key'])
		elif type == "acentry":
			kds.write_string(d['account'])
			kds.write_uint64(d['n'])
			vds.write_int32(d['nVersion'])
			vds.write_int64(d['nCreditDebit'])
			vds.write_int64(d['nTime'])
			vds.write_string(d['otherAccount'])
			vds.write_string(d['comment'])
		else:
			print "Unknown key type: "+type

		# Write the key/value pair to the database
		db.put(kds.input, vds.input)

	except Exception, e:
		print("ERROR writing to wallet.dat, type %s"%type)
		print("data dictionary: %r"%data)
		traceback.print_exc()

def rewrite_wallet(db_env, destFileName, pre_put_callback=None):
	db = open_wallet(db_env)

	db_out = DB(db_env)
	try:
		r = db_out.open(destFileName, "main", DB_BTREE, DB_CREATE)
	except DBError:
		r = True

	if r is not None:
		logging.error("Couldn't open %s."%destFileName)
		sys.exit(1)

	def item_callback(type, d):
		if (pre_put_callback is None or pre_put_callback(type, d)):
			db_out.put(d["__key__"], d["__value__"])

	parse_wallet(db, item_callback)
	db_out.close()
	db.close()

def read_wallet(json_db, db_env, print_wallet, print_wallet_transactions, transaction_filter):
	db = open_wallet(db_env)

	json_db['keys'] = []
	json_db['pool'] = []
	json_db['names'] = {}

	def item_callback(type, d):

		if type == "name":
			json_db['names'][d['hash']] = d['name']

		elif type == "version":
			json_db['version'] = d['version']

		elif type == "setting":
			if not json_db.has_key('settings'): json_db['settings'] = {}
			json_db["settings"][d['setting']] = d['value']

		elif type == "defaultkey":
			json_db['defaultkey'] = public_key_to_bc_address(d['key'])

		elif type == "key":
			addr = public_key_to_bc_address(d['public_key'])
			sec = SecretToASecret(PrivKeyToSecret(d['private_key']))
			private_keys.append(sec)
			json_db['keys'].append({'addr' : addr, 'sec' : sec})

		elif type == "wkey":
			if not json_db.has_key('wkey'): json_db['wkey'] = []
			json_db['wkey']['created'] = d['created']

		elif type == "pool":
			json_db['pool'].append( {'n': d['n'], 'addr': public_key_to_bc_address(d['public_key']), 'nTime' : d['nTime'] } )

		elif type == "acc":
			json_db['acc'] = d['account']
			print("Account %s (current key: %s)"%(d['account'], public_key_to_bc_address(d['public_key'])))

		elif type == "acentry":
			json_db['acentry'] = (d['account'], d['nCreditDebit'], d['otherAccount'], time.ctime(d['nTime']), d['n'], d['comment'])

		elif type == "bestblock":
			json_db['bestblock'] = d['hashes'][0][::-1].encode('hex_codec')

		else:
			json_db[type] = 'unsupported'


	parse_wallet(db, item_callback)

	db.close()

	for k in json_db['keys']:
		addr = k['addr']
		if addr in json_db['names'].keys():
			k["label"] = json_db['names'][addr]
		else:
			k["reserve"] = 1
	
	del(json_db['pool'])
	del(json_db['names'])

def importprivkey(db, sec):
	pkey = regenerate_key(sec)
	if not pkey:
		return False

	secret = GetSecret(pkey)
	private_key = GetPrivKey(pkey)
	public_key = GetPubKey(pkey)
	addr = public_key_to_bc_address(public_key)

	print "Address: %s" % addr
	print "Privkey: %s" % SecretToASecret(secret)

	update_wallet(db, 'key', { 'public_key' : public_key, 'private_key' : private_key })
	update_wallet(db, 'name', { 'hash' : addr, 'name' : '' })

	return True

from optparse import OptionParser

def main():

	global max_version, addrtype

	parser = OptionParser(usage="%prog [options]", version="%prog 0.1")

	parser.add_option("--phrase", dest="keystr", 
		help="convert the passphrase \"KEYSTR\" to a private key base 58 hash")

	(options, args) = parser.parse_args()

	if options.keystr is None:
		print "A mandatory option is missing\n"
		parser.print_help()
		exit(0)

	if options.keystr:
		#Take sha256 hash of key string
		priv_key = hashlib.sha256(options.keystr).digest()
		#Convert hash to bitcoin address
		priv_key = SecretToASecret(priv_key)

		#Make key
		key = regenerate_key(priv_key)

		#Get public key
		publ_key = GetPubKey(key)

		#Get public key address
		bc_add = public_key_to_bc_address(publ_key)

		#Get private key
		privkeyo = GetPrivKey(key)
		
		#Print outputs
		print "Public address: " + bc_add+"\r"
		print "Privey: "+priv_key+"\n"


# Depricated
#Works
		#Exptl alternative to generate private key 
		#Take sha256 hash of key string
#		sha256hash = hashlib.sha256(options.keystr).digest() #sha256hash is the secret
#		padded_add = Add0x80(sha256hash)
#		b58_of_privkey = EncodeBase58Check(padded_add)
#		print b58_of_privkey+"\n" 

		#Get Private Key (alternative method)
#		print "Priv2: " + EncodeBase58Check(Add0x80(GetSecret(key)))+"\n"



#Below may not work


		#Generate address of public key
#		pub_key = GetPubKey(sha256hash)
#		pub_key = regenerate_key(sha256hash).pubkey
#		pub_key = i2o_ECPublicKey(priv_key)
#		print public_key_to_bc_address(pub_key)




#		priv_key = b58encode(SecretToASecret(long_hex(options.keystr)[1:32]))
#		priv_key = SecretToASecret(long_hex(options.keystr)[1:32])
#		priv_key = SecretToASecret(options.keystr)
#		print priv_key+"\n"
#		priv_key = EncodeBase58Check(chr(addrtype+128) + long_hex(options.keystr)[1:32])
#		priv_key = EncodeBase58Check(long_hex(options.keystr))
		#Print 33 byte array
#		priv_key = "0x80"+long_hex(options.keystr)[1:32]
#		pub_key = public_key_to_bc_address(GetPubKey(b58decode(priv_key)))
#		print "Private key: " + priv_key
#		print "Public key: " + pub_key



if __name__ == '__main__':
	main()
