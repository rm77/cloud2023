#!/bin/bash

PARAM=${1-vm0}


mkdir $PARAM > /dev/null 2> /dev/null

if [ "$?" -gt 0 ]; then
	echo "nama $PARAM sudah ada "
	echo "gunakan /script/vm-init.sh [vm1] dst"
	exit 200;
fi

cd $PARAM

cat > .vm-settings <<EOF
VMNAME=$PARAM
VMMACADDRESS=00:00:00:12:34:56
VMMONITORPORT=32001
VMSERIALPORT=32002
VMWEBVNCPORT=32003
VMVNCPORT=1  #mulai dari 5900
VMVNCPASSWORD=$(date | md5sum | cut -d" " -f1 | cut -b1-8)
VMSIZE=1G
VMMEMORY=512
VMCPU=1
VMIMAGELOCATION=/script/vm-lib/images/cloud-image.img
NETBRIDGE=br0
EOF


cat > create.sh <<EOF
   echo "please run update.sh first"
EOF

cat > update.sh <<EOF
#!/bin/sh
. ./.vm-settings
. /script/vm-lib/vm-util.sh
set -x
update_vm_image update
generate_start_vm 
EOF

cat >  down.sh <<EOF
#!/bin/sh
. ./.vm-settings
. /script/vm-lib/vm-util.sh
set -x

if [ -e \$VMNAME.pid ]
then
   kill -9 \$(cat \$VMNAME.pid)
fi

if [ -e WEB\$VMNAME.pid ]
then
   for i in \$(cat WEB\$VMNAME.pid)
   do
     kill -9 \$i
   done
fi

if [ -e WEB\$VMNAME.pgid ]
then
   for i in \$(cat WEB\$VMNAME.pgid)
   do
     pkill -9 -g \$i
   done
fi


EOF


set -x 
. ./.vm-settings
. /script/vm-lib/vm-util.sh
generate_setup_vm_image
update_vm_image new

