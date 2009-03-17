'''CTS: Cluster Testing System: Audit module
 '''

__copyright__='''
Copyright (C) 2000, 2001,2005 Alan Robertson <alanr@unix.sh>
Licensed under the GNU GPL.
'''

#
# This program is free software; you can redistribute it and/or
# modify it under the terms of the GNU General Public License
# as published by the Free Software Foundation; either version 2
# of the License, or (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 59 Temple Place - Suite 330, Boston, MA  02111-1307, USA.

import time, os, popen2, string, re
import CTS
import os
import popen2


class ClusterAudit:

    def __init__(self, cm):
        self.CM = cm

    def __call__(self):
         raise ValueError("Abstract Class member (__call__)")
    
    def is_applicable(self):
        '''Return TRUE if we are applicable in the current test configuration'''
        raise ValueError("Abstract Class member (is_applicable)")
        return 1

    def name(self):
         raise ValueError("Abstract Class member (name)")

AllAuditClasses = [ ]

class LogAudit(ClusterAudit):

    def name(self):
        return "LogAudit"

    def __init__(self, cm):
        self.CM = cm

    def RestartClusterLogging(self, nodes=None):
        if not nodes:
            nodes = self.CM.Env["nodes"]

        self.CM.log("Restarting logging on: %s" % repr(nodes))

        for node in nodes:
            cmd=self.CM.Env["logrestartcmd"]
            if self.CM.rsh(node, cmd, blocking=0) != 0:
                self.CM.log ("ERROR: Cannot restart logging on %s [%s failed]" % (node, cmd))

    def TestLogging(self):
        patterns= []
        prefix="Test message from"
        for node in self.CM.Env["nodes"]:
            # Look for the node name in two places to make sure 
            # that syslog is logging with the correct hostname
            patterns.append("%s.*%s %s" % (node, prefix, node))

        watch = CTS.LogWatcher(self.CM.Env["LogFileName"], patterns, 60)
        watch.setwatch()

        for node in self.CM.Env["nodes"]:
            cmd="logger -p %s.info %s %s" % (self.CM.Env["SyslogFacility"], prefix, node)
            if self.CM.rsh(node, cmd, blocking=0) != 0:
                self.CM.log ("ERROR: Cannot execute remote command [%s] on %s" % (cmd, node))

        watch_result = watch.lookforall()
        if watch.unmatched:
            for regex in watch.unmatched:
                self.CM.log ("Test message [%s] not found in logs." % (regex))
            return 0

        return 1

    def __call__(self):
        max=3
        attempt=0

        self.CM.ns.WaitForAllNodesToComeUp(self.CM.Env["nodes"])
        while attempt <= max and self.TestLogging() == 0:
            attempt = attempt + 1
            self.RestartClusterLogging()
            time.sleep(60*attempt)

        if attempt > max:
            self.CM.log("ERROR: Cluster logging unrecoverable.")
            return 0

        return 1
    
    def is_applicable(self):
        if self.CM.Env["DoBSC"]:
            return 0
        return 1

class DiskAudit(ClusterAudit):

    def name(self):
        return "DiskspaceAudit"

    def __init__(self, cm):
        self.CM = cm

    def __call__(self):
        result=1
        dfcmd="df -k /var/log | tail -1 | tr -s ' ' | cut -d' ' -f2"

        self.CM.ns.WaitForAllNodesToComeUp(self.CM.Env["nodes"])
        for node in self.CM.Env["nodes"]:
            dfout=self.CM.rsh(node, dfcmd, 1)
            if not dfout:
                self.CM.log ("ERROR: Cannot execute remote df command [%s] on %s" % (dfcmd, node))
            else:
                try:
                    idfout = int(dfout)
                except (ValueError, TypeError):
                    self.CM.log("Warning: df output from %s was invalid [%s]" % (node, dfout))
                else:
                    if idfout == 0:
                        self.CM.log("CRIT: Completely out of log disk space on %s" % node)
                        result=None
                    elif idfout <= 1000:
                        self.CM.log("WARN: Low on log disk space (%d Mbytes) on %s" % (idfout, node))
        return result
    
    def is_applicable(self):
        if self.CM.Env["DoBSC"]:
            return 0
        return 1

class AuditResource:
    def __init__(self, cm, line):
        fields = line.split()
        self.CM = cm
        self.line = line
        self.type = fields[1]
        self.id = fields[2]
        self.clone_id = fields[3]
        self.parent = fields[4]
        self.managed = fields[5]
        self.needs_quorum = fields[6]
        self.unique = fields[7]
        self.rprovider = fields[8]
        self.rclass = fields[9]
        self.rtype = fields[10]

        if self.parent == "NA":
            self.parent = None

class AuditConstraint:
    def __init__(self, cm, line):
        fields = line.split()
        self.CM = cm
        self.line = line
        self.type = fields[1]
        self.id = fields[2]
        self.rsc = fields[3]
        self.target = fields[4]
        self.score = fields[5]
        self.rsc_role = fields[6]
        self.target_role = fields[7]

        if self.rsc_role == "NA":
            self.rsc_role = None
        if self.target_role == "NA":
            self.target_role = None

class PrimitiveAudit(ClusterAudit):
    def name(self):
        return "PrimitiveAudit"

    def __init__(self, cm):
        self.CM = cm

    def doResourceAudit(self, resource):
        rc=1
        active = self.CM.ResourceLocation(resource.id)

        if len(active) > 1:
            if not resource.unique == "1":
                rc=0
                self.CM.log("Resource %s is active multiple times: %s" 
                            % (resource.id, repr(active)))
            else:
                self.CM.debug("Non-unique resource %s is active on: %s" 
                              % (resource.id, repr(active)))
            
        elif len(active) == 1:
            if self.CM.HasQuorum(None):
                self.CM.debug("Resource %s active on %s" % (resource.id, repr(active)))
                
            elif resource.needs_quorum == 1:
                rc=0
                self.CM.log("Resource %s active without quorum: %s (%s)" 
                            % (resource.id, repr(active), resource.line))

        elif self.CM.Env["warn-inactive"] == 1:
            if self.CM.HasQuorum(None) or not resource.needs_quorum:
                self.CM.log("WARN: Resource %s not served anywhere (Inactive nodes: %s)" 
                            % (resource.id, repr(self.inactive_nodes)))
            else:
                self.CM.debug("Resource %s not served anywhere (Inactive nodes: %s)" 
                              % (resource.id, repr(self.inactive_nodes)))

        elif self.CM.HasQuorum(None) or not resource.needs_quorum:
            self.CM.debug("Resource %s not served anywhere (Inactive nodes: %s)" 
                          % (resource.id, repr(self.inactive_nodes)))

        if resource.managed == "0":
            self.CM.log("Resource %s not managed: faking success" % resource.id)
            rc = 1

        return rc

    def setup(self):
        self.target = None
        self.resources = []
        self.constraints = []
        self.active_nodes = []
        self.inactive_nodes = []

        self.CM.debug("Do Audit %s"%self.name())

        for node in self.CM.Env["nodes"]:
            if self.CM.ShouldBeStatus[node] == "up":
                self.active_nodes.append(node)
            else:
                self.inactive_nodes.append(node)

        for node in self.CM.Env["nodes"]:
            if self.target == None and self.CM.ShouldBeStatus[node] == "up":
                self.target = node

        if not self.target:
            # TODO: In Pacemaker 1.0 clusters we'll be able to run crm_resource 
            # with CIB_file=/path/to/cib.xml even when the cluster isn't running
            self.CM.debug("No nodes active - skipping %s" % self.name())
            return 0

        (rc, lines) = self.CM.rsh(self.target, "crm_resource -c", None)

        for line in lines:
            if re.search("^Resource", line):
                self.resources.append(AuditResource(self.CM, line))
            elif re.search("^Constraint", line):
                self.constraints.append(AuditConstraint(self.CM, line))
            else:
                self.CM.log("Unknown entry: %s" % line);

        return 1

    def __call__(self):
        rc = 1
                
        if not self.setup():
            return 1

        for resource in self.resources:
            if resource.type == "primitive":
                if self.doResourceAudit(resource) == 0:
                    rc = 0
        return rc

    def is_applicable(self):
        if self.CM["Name"] == "crm-lha":
            return 1
        if self.CM["Name"] == "crm-ais":
            return 1
        return 0

class GroupAudit(PrimitiveAudit):
    def name(self):
        return "GroupAudit"

    def __call__(self):
        rc = 1
        if not self.setup():
            return 1

        for group in self.resources:
            if group.type == "group":
                first_match = 1
                group_location = None
                for child in self.resources:
                    if child.parent == group.id:
                        nodes = self.CM.ResourceLocation(child.id)

                        if first_match and len(nodes) > 0:
                            group_location = nodes[0]

                        first_match = 0

                        if len(nodes) > 1:
                            rc = 0
                            self.CM.log("Child %s of %s is active more than once: %s" 
                                        % (child.id, group.id, repr(nodes)))

                        elif len(nodes) == 0:
                            # Groups are allowed to be partially active
                            # However we do need to make sure later children aren't running
                            group_location = None
                            self.CM.debug("Child %s of %s is stopped" % (child.id, group.id))

                        elif nodes[0] != group_location:  
                            rc = 0
                            self.CM.log("Child %s of %s is active on the wrong node (%s) expected %s" 
                                        % (child.id, group.id, nodes[0], group_location))
                        else:
                            self.CM.debug("Child %s of %s is active on %s" % (child.id, group.id, nodes[0]))

        return rc
    
class CloneAudit(PrimitiveAudit):
    def name(self):
        return "CloneAudit"

    def __call__(self):
        rc = 1
        if not self.setup():
            return 1

        for clone in self.resources:
            if clone.type == "clone":
                for child in self.resources:
                    if child.parent == clone.id and child.type == "primitive":
                        self.CM.debug("Checking child %s of %s..." % (child.id, clone.id))
                        # Check max and node_max
                        # Obtain with:
                        #    crm_resource -g clone_max --meta -r child.id
                        #    crm_resource -g clone_node_max --meta -r child.id

        return rc
    
class ColocationAudit(PrimitiveAudit):
    def name(self):
        return "ColocationAudit"

    def crm_location(self, resource):
        (rc, lines) = self.CM.rsh(self.target, "crm_resource -W -r %s -Q"%resource, None)
        hosts = []
        if rc == 0:
            for line in lines:
                fields = line.split()
                hosts.append(fields[0])

        return hosts

    def __call__(self):
        rc = 1
        if not self.setup():
            return 1

        for coloc in self.constraints:
            if coloc.type == "rsc_colocation":
                source = self.crm_location(coloc.rsc)
                target = self.crm_location(coloc.target)
                if len(source) == 0:
                    self.CM.debug("Colocation audit (%s): %s not running" % (coloc.id, coloc.rsc))
                else:
                    for node in source:
                        if not node in target:
                            rc = 0
                            self.CM.log("Colocation audit (%s): %s running on %s (not in %s)" 
                                        % (coloc.id, coloc.rsc, node, repr(target)))
                        else:
                            self.CM.debug("Colocation audit (%s): %s running on %s (in %s)" 
                                          % (coloc.id, coloc.rsc, node, repr(target)))

        return rc

class CrmdStateAudit(ClusterAudit):
    def __init__(self, cm):
        self.CM = cm
        self.Stats = {"calls":0
        ,        "success":0
        ,        "failure":0
        ,        "skipped":0
        ,        "auditfail":0}

    def has_key(self, key):
        return self.Stats.has_key(key)

    def __setitem__(self, key, value):
        self.Stats[key] = value
        
    def __getitem__(self, key):
        return self.Stats[key]

    def incr(self, name):
        '''Increment (or initialize) the value associated with the given name'''
        if not self.Stats.has_key(name):
            self.Stats[name]=0
        self.Stats[name] = self.Stats[name]+1

    def __call__(self):
        passed = 1
        up_are_down = 0
        down_are_up = 0
        unstable_list = []
        self.CM.debug("Do Audit %s"%self.name())

        for node in self.CM.Env["nodes"]:
            should_be = self.CM.ShouldBeStatus[node]
            rc = self.CM.test_node_CM(node)
            if rc > 0:
                if should_be == "down":
                    down_are_up = down_are_up + 1
                if rc == 1:
                    unstable_list.append(node)
            elif should_be == "up":
                up_are_down = up_are_down + 1

        if len(unstable_list) > 0:
            passed = 0
            self.CM.log("Cluster is not stable: %d (of %d): %s" 
                     %(len(unstable_list), self.CM.upcount(), repr(unstable_list)))

        if up_are_down > 0:
            passed = 0
            self.CM.log("%d (of %d) nodes expected to be up were down."
                     %(up_are_down, len(self.CM.Env["nodes"])))

        if down_are_up > 0:
            passed = 0
            self.CM.log("%d (of %d) nodes expected to be down were up." 
                     %(down_are_up, len(self.CM.Env["nodes"])))
            
        return passed

    def name(self):
        return "CrmdStateAudit"
    
    def is_applicable(self):
        if self.CM["Name"] == "crm-lha":
            return 1
        if self.CM["Name"] == "crm-ais":
            return 1
        return 0

class CIBAudit(ClusterAudit):
    def __init__(self, cm):
        self.CM = cm
        self.Stats = {"calls":0
        ,        "success":0
        ,        "failure":0
        ,        "skipped":0
        ,        "auditfail":0}

    def has_key(self, key):
        return self.Stats.has_key(key)

    def __setitem__(self, key, value):
        self.Stats[key] = value
        
    def __getitem__(self, key):
        return self.Stats[key]
    
    def incr(self, name):
        '''Increment (or initialize) the value associated with the given name'''
        if not self.Stats.has_key(name):
            self.Stats[name]=0
        self.Stats[name] = self.Stats[name]+1

    def __call__(self):
        self.CM.debug("Do Audit %s"%self.name())
        passed = 1
        ccm_partitions = self.CM.find_partitions()

        if len(ccm_partitions) == 0:
            self.CM.debug("\tNo partitions to audit")
            return 1
        
        for partition in ccm_partitions:
            self.CM.debug("\tAuditing CIB consistency for: %s" %partition)
            partition_passed = 0
            if self.audit_cib_contents(partition) == 0:
                passed = 0
        
        return passed

    def audit_cib_contents(self, hostlist):
        passed = 1
        first_host = None
        first_host_xml = ""
        partition_hosts = hostlist.split()
        for a_host in partition_hosts:
                if first_host == None:
                        first_host = a_host
                        first_host_xml = self.store_remote_cib(a_host)
                        #self.CM.debug("Retrieved CIB: %s" % first_host_xml) 
                else:
                        a_host_xml = self.store_remote_cib(a_host)
                        diff_cmd="crm_diff -c -VV -f -N \'%s\' -O '%s'" % (a_host_xml, first_host_xml)

                        infile, outfile, errfile = os.popen3(diff_cmd)
                        diff_lines = outfile.readlines()
                        for line in diff_lines:
                            if not re.search("<diff/>", line):
                                passed = 0
                                self.CM.debug("CibDiff[%s-%s]: %s" 
                                            % (first_host, a_host, line)) 
                            else:
                                self.CM.debug("CibDiff[%s-%s] Ignoring: %s" 
                                              % (first_host, a_host, line)) 

                        diff_lines = errfile.readlines()
                        for line in diff_lines:
                            passed = 0
                            self.CM.log("CibDiff[%s-%s] ERROR: %s" 
                                        % (first_host, a_host, line)) 

        return passed
                
    def store_remote_cib(self, node):
        combined = ""
        first_line = 1
        extra_debug = 0
        #self.CM.debug("\tRetrieving CIB from: %s" % node)
        (rc, lines) = self.CM.rsh(node, self.CM["CibQuery"], None)
        if extra_debug:
            self.CM.debug("Start Cib[%s]" % node) 
        for line in lines:
            combined = combined + line[:-1]
            if first_line:
                self.CM.debug("[Cib]" + line)
                first_line = 0
            elif extra_debug:
                self.CM.debug("[Cib]" + line) 

        if extra_debug:
            self.CM.debug("End Cib[%s]" % node) 

        #self.CM.debug("Complete CIB: %s" % combined) 
        return combined

    def name(self):
        return "CibAudit"
    
    def is_applicable(self):
        if self.CM["Name"] == "crm-lha":
            return 1
        if self.CM["Name"] == "crm-ais":
            return 1
        return 0

class PartitionAudit(ClusterAudit):
    def __init__(self, cm):
        self.CM = cm
        self.Stats = {"calls":0
        ,        "success":0
        ,        "failure":0
        ,        "skipped":0
        ,        "auditfail":0}
        self.NodeEpoche={}
        self.NodeState={}
        self.NodeQuorum={}

    def has_key(self, key):
        return self.Stats.has_key(key)

    def __setitem__(self, key, value):
        self.Stats[key] = value
        
    def __getitem__(self, key):
        return self.Stats[key]
    
    def incr(self, name):
        '''Increment (or initialize) the value associated with the given name'''
        if not self.Stats.has_key(name):
            self.Stats[name]=0
        self.Stats[name] = self.Stats[name]+1

    def __call__(self):
        self.CM.debug("Do Audit %s"%self.name())
        passed = 1
        ccm_partitions = self.CM.find_partitions()

        if ccm_partitions == None or len(ccm_partitions) == 0:
            return 1

        if len(ccm_partitions) != self.CM.partitions_expected:
            self.CM.log("ERROR: %d cluster partitions detected:" %len(ccm_partitions))
            passed = 0
            for partition in ccm_partitions:
                self.CM.log("\t %s" %partition)

        for partition in ccm_partitions:
            partition_passed = 0
            if self.audit_partition(partition) == 0:
                passed = 0

        return passed

    def trim_string(self, avalue):
        if not avalue:
            return None
        if len(avalue) > 1:
            return avalue[:-1]

    def trim2int(self, avalue):
        if not avalue:
            return None
        if len(avalue) > 1:
            return int(avalue[:-1])

    def audit_partition(self, partition):
        passed = 1
        dc_found = []
        dc_allowed_list = []
        lowest_epoche = None
        node_list = partition.split()

        self.CM.debug("Auditing partition: %s" %(partition))
        for node in node_list:
            if self.CM.ShouldBeStatus[node] != "up":
                self.CM.log("Warn: Node %s appeared out of nowhere" %(node))
                self.CM.ShouldBeStatus[node] = "up"
                # not in itself a reason to fail the audit (not what we're
                #  checking for in this audit)

            self.NodeState[node]  = self.CM.rsh(node, self.CM["StatusCmd"]%node, 1)
            self.NodeEpoche[node] = self.CM.rsh(node, self.CM["EpocheCmd"], 1)
            self.NodeQuorum[node] = self.CM.rsh(node, self.CM["QuorumCmd"], 1)
            
            self.CM.debug("Node %s: %s - %s - %s." %(node, self.NodeState[node], self.NodeEpoche[node], self.NodeQuorum[node]))
            self.NodeState[node]  = self.trim_string(self.NodeState[node])
            self.NodeEpoche[node] = self.trim2int(self.NodeEpoche[node])
            self.NodeQuorum[node] = self.trim_string(self.NodeQuorum[node])

            if not self.NodeEpoche[node]:
                self.CM.log("Warn: Node %s dissappeared: cant determin epoche" %(node))
                self.CM.ShouldBeStatus[node] = "down"
                # not in itself a reason to fail the audit (not what we're
                #  checking for in this audit)
            elif lowest_epoche == None or self.NodeEpoche[node] < lowest_epoche:
                lowest_epoche = self.NodeEpoche[node]
                
        if not lowest_epoche:
            self.CM.log("Lowest epoche not determined in %s" % (partition))
            passed = 0

        for node in node_list:
            if self.CM.ShouldBeStatus[node] == "up":
                if self.CM.is_node_dc(node, self.NodeState[node]):
                    dc_found.append(node)
                    if self.NodeEpoche[node] == lowest_epoche:
                        self.CM.debug("%s: OK" % node)
                    elif not self.NodeEpoche[node]:
                        self.CM.debug("Check on %s ignored: no node epoche" % node)
                    elif not lowest_epoche:
                        self.CM.debug("Check on %s ignored: no lowest epoche" % node)
                    else:
                        self.CM.log("DC %s is not the oldest node (%d vs. %d)"
                            %(node, self.NodeEpoche[node], lowest_epoche))
                        passed = 0

        if len(dc_found) == 0:
            self.CM.log("DC not found on any of the %d allowed nodes: %s (of %s)"
                        %(len(dc_allowed_list), str(dc_allowed_list), str(node_list)))

        elif len(dc_found) > 1:
            self.CM.log("%d DCs (%s) found in cluster partition: %s"
                        %(len(dc_found), str(dc_found), str(node_list)))
            passed = 0

        if passed == 0:
            for node in node_list:
                if self.CM.ShouldBeStatus[node] == "up":
                    self.CM.log("epoche %s : %s"  
                                %(self.NodeEpoche[node], self.NodeState[node]))


        return passed

    def name(self):
        return "PartitionAudit"
    
    def is_applicable(self):
        if self.CM["Name"] == "crm-lha":
            return 1
        if self.CM["Name"] == "crm-ais":
            return 1
        return 0

AllAuditClasses.append(DiskAudit)
AllAuditClasses.append(LogAudit)
AllAuditClasses.append(CrmdStateAudit)
AllAuditClasses.append(PartitionAudit)
AllAuditClasses.append(PrimitiveAudit)
AllAuditClasses.append(GroupAudit)
AllAuditClasses.append(CloneAudit)
AllAuditClasses.append(ColocationAudit)
AllAuditClasses.append(CIBAudit)

def AuditList(cm):
    result = []
    for auditclass in AllAuditClasses:
        result.append(auditclass(cm))
    return result