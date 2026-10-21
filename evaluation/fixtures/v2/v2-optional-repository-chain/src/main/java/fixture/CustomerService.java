package fixture;

public class CustomerService {
    private final CustomerRepository customers;
    private final AddressRepository addresses;

    public CustomerService(CustomerRepository customers, AddressRepository addresses) {
        this.customers = customers;
        this.addresses = addresses;
    }

    public CustomerDto load(int id) {
        String name = customers.findName(id).get();
        String city = addresses.findCity(id).get();
        return new CustomerDto(name, city);
    }
}
