**Environment**

disediakan 1 buah mesin dengan web based terminal
- mesin-1


untuk menjalankan dibutuhkan
- docker
- docker-compose
- docker image royyana/vmdev:2.00


> [!NOTE]
> royyana/vmdev:2.00 merupakan docker image yang telah dilengkapi dengan berbagai macam tool yang ready to use untuk keperluan MK komputasi awan
> berisi diantaranya adalah qemu, ip, firecracker, kubectl, docker, docker-compose 

menjalankan lab
```
cd environment
docker-compose up -d
```

> [!NOTE] 
> Lokasi penting didalam container
> webbased terminal dapat diakses di 
> _http://[ipaddress]:10001_ dengan  username:user1 password:mesin1
> - direktori __work__  __/home/work__
> - direktori script __/script/__

untuk menghentikan lab
```
docker-compose down
```


**Provisioning VM**
- Membuat virtual switch/bridge

```
cd /home/work
```
pindah ke tempat default untuk home direktori


```
sh /script/bridge-init.sh
```
untuk menhghasilkan script yang dibutuhkan dalam provisioning virtual switch / bridge untuk menghubungkan antar VM
akan menghasilkan direktori br0 (by default) atau yang lain , jika diinginkan
di dalam direktori tersebut akan terdapat beberapa script automasi untuk
- mengaktifkan bridge, mendaftarkan rule firewall, setting NAT, dan mengeset DHCP (dnsmasq)
- menonaktifkan bridge dan segala pendukungnya
- menonaktifkan tap interface

```
cd br0
sh create.sh
```

untuk mengaktifkan bridge dengan id br0
cek dengan 
- ps ax , akan terlihat process dnsmasq
- ifconfig br0
- iptables -L -n 

untuk menonaktifkan dan menghapus bridge
```
sh delete.sh
```

**Membuat VM**
```
cd /home/work
sh /script/vm-init.sh
```
untuk menghasilkan script yang dibutuhkan dalam provisioning VM (qemu). by default adalah vm0
provisioning membutuhkan otomasi
- menjalankan qemu
- menjalankan webbased vnc terminal
```
cd vm0
```
sesuaikan isi file .vm-settings untuk mengeset beberapa parameter seperti macaddress dan port serta vcpu dan storage
```
sh update.sh 
```
update.sh melakukan otomasi untuk mengenerate boot image dan storage
```
sh create.sh
```
untuk menjalankan qemu dan perlengkapannya (web based vnc terminal). pada saat berjalan, vm dapat diakses dari web based terminal berbasis VNC dengan password
ip didalam VM akan bergantung dari switch/bridge setting yang digunakan
```
sh down.sh
```
untuk menghentikan qemu/vm, untuk menjalankan lagi jalankan create.sh lagi


