while read ip
do
sudo iptables -A INPUT -s $ip -j DROP
done < /tmp/ip_block_list.txt

#sudo apt install iptables-persistent -y
#sudo netfilter-persistent save
#sudo /usr/sbin/netfilter-persistent save
