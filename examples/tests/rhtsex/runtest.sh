#!/bin/sh

# Copyright (c) 2009 Red Hat, Inc. All rights reserved.
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 59 Temple Place, Suite 330, Boston, MA  02111-1307  USA
#
# Author: Marian Csontos <mcsontos@redhat.com>

# source the test script helpers 

# FIXME: See also
# ${BEAKER_ROOT}/beakerlib/examples/phases/runtest.sh
# ${BEAKER_ROOT}/beakerlib/test/beakerlibTest.sh

. /usr/bin/rhts-environment.sh

echo "--- start of runtest.sh ---" | tee -a $OUTPUTFILE

echo "--- start environment ---" | tee -a $OUTPUTFILE
set | tee -a $OUTPUTFILE
echo "--- --- --- --- ---" | tee -a $OUTPUTFILE
export | tee -a $OUTPUTFILE
echo "--- end environment ---" | tee -a $OUTPUTFILE

echo "rhts-abort [-l SERVER] (-t recipe [-r RECIPEID]|-t recipeset [-s RECIPESETID]|-t job [-j JOBID])"
rhts-abort
rhts-abort -t recipe -l $RESULT_SERVER
rhts-abort -t recipe -r 65432
rhts-abort -t recipeset
rhts-abort -t recipeset -s 65432 -l $RESULT_SERVER
rhts-abort -t job
rhts-abort -t job -j 65432
rhts-abort -t job -r 65432
rhts-abort -t job -s 65432

echo "rhts-client [-u SUBMITTER] [-S SERVER] RECIPE_ID COMMENT"
rhts-client
rhts-client 1 "a comment"
rhts-client -u mcsontos@redhat.com $RECIPEID "a comment"
rhts-client -S $RESULT_SERVER 1 "a comment"

# FIXME: 
cat <<EOF
rhts-db-submit-result (-S|--server) SERVER (-t|--testname) TESTNAME
    [(-T|--testid) RECIPETESTID] [(-r|--result) RESULT]
    [(-v|--resultvalue) RESULT_VALUE]
    [(-D|--dmesg) DMESG_FILE] [(-L|--log) LOG_FILE] [-d|--debug]
EOF
rhts-db-submit-result
rhts-db-submit-result -S $RESULT_SERVER
rhts-db-submit-result -t $TEST
rhts-db-submit-result -S $RESULT_SERVER -t $TEST
rhts-db-submit-result -S $RESULT_SERVER -t $TEST -r Pass
rhts-db-submit-result -S $RESULT_SERVER -t $TEST -v 12.34
rhts-db-submit-result -S $RESULT_SERVER -t $TEST -l `mktemp`
rhts-db-submit-result -S $RESULT_SERVER -t $TEST -D `mktemp`
rhts-db-submit-result -S $RESULT_SERVER -t $TEST -d

echo "rhts-recipe-sync-block [-R|--result_server SERVER] [-r|--recipesetid RECIPESETID] (-s|--state STATE)+ MACHINE+"
rhts-recipe-sync-block
rhts-recipe-sync-block -s DONE $HOSTNAME && \
rhts-recipe-sync-block -s STAT1 -s STAT2 host1 host2 && \
rhts-recipe-sync-block -R $RESULT_SERVER -r $RECIPESETID -s STAT1 -s STAT2 host1 host2 &

echo "rhts-recipe-sync-set [-R|--result_server SERVER] [-r|--recipesetid RECIPESETID] [-m MACHINE] (-s|--state STATE)"
rhts-recipe-sync-set
rhts-recipe-sync-set -s WAIT
rhts-recipe-sync-set -s DONE
rhts-recipe-sync-set -m $HOSTNAME -s DONE
rhts-recipe-sync-set -r $RECIPESETID -s DONE
rhts-recipe-sync-set -R $RESULT_SERVER -m host1 -s STAT1
rhts-recipe-sync-set -m host2 -r $RECIPESETID -R $RESULT_SERVER -s STAT2

echo "rhts-reboot"
# I have not intentions running this...
# Actually, it is scary it will reboot without notifying server.
# FIXME: beaker should provide a replacement

echo "rhts-report-result TEST RESULT LOGFILE [METRIC]"
rhts-report-result
rhts-report-result $TEST Pass `mktemp`
rhts-report-result $TEST Warn `mktemp`
rhts-report-result $TEST Fail `mktemp` 12.34

echo "rhts-submit-log [-S SERVER] [-T RECIPETESTID] -l LOGFILE"
rhts-submit-log
rhts-submit-log -l `mktemp`
rhts-submit-log -S $RESULT_SERVER -T $RECIPETESTID -l `mktemp`

echo "rhts-sync-block [-R|--result_server SERVER] [-r|--recipesetid RECIPESETID] [-t|--testorder TESTORDER] (-s|--state STATE) MACHINE+"
rhts-sync-block
rhts-sync-block -s DONE2 $HOSTNAME && \
rhts-sync-block -R $RESULT_SERVER -s STAT3 -s STAT4 host3 host4 && \
rhts-sync-block -t $TESTORDER -s STAT3 -s STAT4 host3 host4 && \
rhts-sync-block -r $RECIPESETID -s STAT3 -s STAT4 host3 host4 &

echo "rhts-sync-set [-R|--result_server SERVER] [-r|--recipesetid RECIPESETID] [-t|--testorder TESTORDER] [-m MACHINE] (-s|--state STATE)"
rhts-sync-set
rhts-sync-set -s WAIT2
rhts-sync-set -t $TESTORDER -s DONE2
rhts-sync-set -m $HOSTNAME -s DONE2
rhts-sync-set -t $TESTORDER -r $RECIPESETID -s DONE2
rhts-sync-set -R $RESULT_SERVER -m host3 -s STAT3
rhts-sync-set -t $TESTORDER -m host4 -r $RECIPESETID -R $RESULT_SERVER -s STAT4

echo "report_result TEST RESULT"
report_result
report_result $TEST PASS

echo "--- end of runtest.sh ---" | tee -a $OUTPUTFILE
