while read port
do
sudo iptables -A INPUT -p tcp --dport $port -j DROP
done < /tmp/port_block_list.txt
