disediakan 3 buah mesin dengan web based terminal
- mesin-1
- mesin-2
- mesin-3


untuk menjalankan dibutuhkan
- docker
- docker-compose


menjalankan lab
- docker-compose up -d

webbased terminal dapat diakses di 
- mesin1 http://[ipaddress]:60001 password: mesin1
- mesin1 http://[ipaddress]:60002 password: mesin2
- mesin1 http://[ipaddress]:60003 password: mesin3

untuk setiap mesin, lokasi direktori yang dapat dimapping adalah
- mesin1 -> workmesin1 --> /home/jovyan/work
- mesin2 -> workmesin2 --> /home/jovyan/work
- mesin3 -> workmesin3 --> /home/jovyan/work

untuk menghentikan lab
- docker-compose down
