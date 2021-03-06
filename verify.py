#!/usr/bin/python
# -*- coding: utf-8 -*-

import requests
import time
import argparse
import re
import sys
import sqlite3

class StsPolicy(object): # {{{
    """docstring for StsPolicy"""
    def __init__(self, domain, sts_record, dnssec=False, cached_since=0):
        super(StsPolicy, self).__init__()

        self.v   = None
        self.a   = None
        self.c   = None
        self.e   = None
        self.to  = None
        self.rua = None
        self.mx  = None

        self.cached_since   = cached_since
        self.now            = int(time.time())
        self.expires        = 0
        self.domain         = domain
        self.sts_record     = unicode(sts_record)
        self.via_dnssec     = dnssec
        self.is_policy      = False

        try:
            self.__split_up()
            self.is_policy = True
        except:
            # TODO: propper exception handling
            pass

    def get_policy(self):
        """returns all the policy data"""
        if self.is_policy:
            content = (
                self.v,
                self.a,
                self.c,
                self.e,
                self.to,
                self.rua,
                self.mx
            )
        else:
            content = False
        return content

    def expired(self):
        """checks if the policy is expired"""

        if self.cached_since + self.e > self.now:
            return False
        else:
            return True

    def __split_up(self):
        """splits up (parses) the _smtp-sts TXT-RR"""
        # TODO: Error handling everywhere.

        record =  {}
        y = [ x.strip() for x in self.sts_record.split(';') ]

        for x in y:
            (key, value) = x.split('=', 1)
            record[key]=value

        # v: Version (plain-text, required).  Currently only "STS1" is supported
        if record['v'] != "STS1":
            return False

        self.v = record['v']

        # e: Max lifetime of the policy (plain-text integer seconds)
        self.e = int(record['e'])
        self.expires = self.now + self.e

        # mx: MX patterns (comma-separated list of plain-text MX match patterns)
        self.mx = record['mx'].split(',')

        # to: TLS-Only (plain-text, required). If "true" the receiving MTA...
        if record['to'] == 'true':
            self.to = True
        else:
            self.to = False

        # a: The mechanism to use to authenticate this policy itself.
        if record['a'][0:6] == 'dnssec':
            self.a = { 'dnssec': self.via_dnssec } # None == unvalidated
        elif record['a'][0:6] == 'webpki':
            try:
                self.a = { 'webpki': 'https://%s/%s' % (self.domain, record['a'].split(':',1)[1], ) }
            except:
                self.a = { 'webpki': 'https://%s/.well-known/smtp-sts/current' % (self.domain,) }

        # c: Constraints on the recipient MX's TLS certificate
        if record['c'] not in ( 'webpki', 'tlsa', ):
            return False
        self.c = record['c']

        self.rua = record['rua']

        return True

# }}}

class SmtpSts(object): # {{{
    """docstring for SmtpSts"""
    def __init__(self, domain, mx_records, sts_record, cachedb_file, verbose=False):
        super(SmtpSts, self).__init__()
        self.domain     = domain

        self.sts_domain = "_smtp-sts.%s" % ( domain, )
        self.sts_record = sts_record
        self.mx_records = mx_records
        self.verbose    = verbose
        self.output     = ''
        self.__cachedb  = sqlite3.connect(cachedb_file)

        # create sqlitedb
        try:
            c = self.__cachedb.cursor()
            c.execute('CREATE TABLE sts_cache (domain text, tls_only text, sts_record text, expires int)')
            self.__cachedb.commit()
            print "created DB"
        except:
            pass


    def policy_from_cache(self):
        """get the policy from the cache"""
        p = False
        c = self.__cachedb.cursor()
        c.execute('SELECT sts_record, expires FROM sts_cache WHERE domain=?', ( self.domain, ))
        result = c.fetchone()
        if result:
            p = StsPolicy( domain = self.domain, sts_record = result[0], cached_since = result[1] )
            self.output += "Cached; "
        return p

    def cache(self, policy):
        """cache this policy"""
        c = self.__cachedb.cursor()
        print policy.to
        # I don't like updates.
        # TODO: use updates and make `domain` a primary key
        c.execute('INSERT INTO sts_cache ( domain, tls_only, sts_record, expires ) VALUES (?, ?, ?, ?)',
                ( policy.domain, str(policy.to), policy.sts_record, policy.expires, ) )
        c.execute('DELETE from sts_cache WHERE domain = ? AND expires < ? ', ( policy.domain, policy.now, ) )
        self.__cachedb.commit()
        self.output += "Updated Cache; "

    def policy_from_dns(self):
        """get the policy from DNS"""
        p = StsPolicy( domain = self.domain, sts_record = self.sts_record )
        return p

    def policy_from_webpki(self, uri):
        """get the policy from WebPKI"""
        # TODO: Exceptionhandling, if SSL fails, don't crash.
        sts_record = requests.get(uri).text
        p = StsPolicy(self.domain, sts_record)
        if self.verbose: print "got webpki;"
        self.output += "got webpki; "
        return p

    def validate_mx(self, policy):
        """validate the MX againts the policy"""
        r_MX = policy.mx
        d_MX = self.mx_records

        r_MX_patterns = {}

        # Build regex_patterns
        for r_mx in r_MX:
            regex_p = '%s.?$' % ( re.sub('\.', '\.', re.sub('_', '^[a-z0-1-]+' ,r_mx, 1)), )
            r_MX_patterns[r_mx] = re.compile(regex_p)

        passed = False

        for d_mx in d_MX:
            for r_mx in r_MX:
                if r_MX_patterns[r_mx].match(d_mx):
                    passed = True
                    #if self.verbose: print 'OK: "%s" matches "%s"' % (d_mx, r_mx, )
                    if self.verbose: print 'MX matches policy; '
                    #self.output += 'OK: "%s" matches "%s"; ' % (d_mx, r_mx, )
                    self.output += 'MX matches policy; '
                else:
                    if self.verbose: print 'FAIL: "%s" does not match "%s"' % (d_mx, r_mx, )
                    self.output += 'FAIL: "%s" does not match "%s"' % (d_mx, r_mx, )

        return passed


    def validate(self):
        """validate the policies"""
        update_cache = False
        return_code = 0

        policy = self.policy_from_cache()
        if not policy or policy.expired():
            if not policy:
                if self.verbose: print "no cache"
                self.output += "no cache; "
            elif policy.expired():
                if self.verbose: print "cache expired"
                self.output += "cache expired; "

            dns = self.policy_from_dns()

            if dns.is_policy:
                if self.verbose: print "policy in DNS;"
                self.output += "policy in DNS; "
                if dns.a['webpki']: # Authenticate via WebPKI
                    auth = self.policy_from_webpki(dns.a['webpki'])

                    if dns.get_policy() == auth.get_policy():
                        if self.verbose: print "DNS and WebPKI match;"
                        self.output += "DNS and WebPKI match; "
                        # if they match, dns is the new policy
                        policy = dns
                        update_cache = True
                    else:
                        if self.verbose: print "FAIL: DNS and WebPKI mismatch;"
                        self.output += "FAIL: DNS and WebPKI mismatch; "
                        return False

                elif dns.a['dnssec']: # Authenticate via dnssec
                    self.output += "DNS and DNSSEC match; "

                else:
                    self.output += "FAIL: DNS and ???? don't mismatch; "
                    return False

            else:
                self.output += "No STS Policy; "
                return True

        else:
            if self.verbose: print "cache OK"

        if self.validate_mx(policy):
            if update_cache:
                self.cache(policy)
            return True

        return False

# }}}

if __name__ == "__main__":

    parser = argparse.ArgumentParser(
        description='Verify SMTP-STS',
        formatter_class=argparse.ArgumentDefaultsHelpFormatter
    )
    parser.add_argument( '-d', '--domain',      metavar='D',    type=str,   help='domain to test',          required=True )
    parser.add_argument( '-s', '--smtp-sts',    metavar='TXT',  type=str,   help='_smtp-sts TXT record',    required=True )
    parser.add_argument( '-m', '--mx',          metavar='MX',   type=str,   help='MXes',                    required=True, action="append" )
    parser.add_argument( '-c', '--cachedb',     metavar='FILE', type=str,   help='sqlite3 cachedb',         default="/var/tmp/smtp-sts-cache.db" )
    parser.add_argument( '-D', '--dnssec',                                  help='DNSSEC was used used',    action='store_true' )
    parser.add_argument( '-v', '--verbose',                                 help='verbose output',          action='store_true' )
    args = parser.parse_args()

    s = SmtpSts(args.domain, args.mx, args.smtp_sts, args.cachedb, args.verbose)

    # TODO: different return-codes for different errors
    if s.validate():
        print s.output
        sys.exit(0)
    else:
        print s.output
        sys.exit(1)

# vim:fdm=marker:ts=4:sw=4:sts=4:ai:sta:et
